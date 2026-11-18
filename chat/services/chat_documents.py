"""A chat session's Temporal documents.

When a document is summarized/saved through Temporal, the chat remembers only the file's
name and the workflow id, as a CONTEXT message of kind `document_workflow`:

    {"kind": "document_workflow", "filename": ..., "stored_path": ...,
     "document_id": ..., "workflow_id": ...}

The summary and the knowledge-base status live in the workflow itself and are read back
with a query. This module is the one place that reads/writes those rows; it imports no
tools so both the tools and PromptService can use it.
"""
import asyncio
import json

from asgiref.sync import sync_to_async

from chat.models import Message, MessageRole, MessageType
from document.temporal import client as temporal

DOCUMENT_WORKFLOW_KIND = 'document_workflow'
STATE_TIMEOUT = 4  # seconds, per document


def record_workflow(conversation, document, workflow_id, filename, stored_path):
    """Remembers the workflow in this chat's history (once per workflow id)."""
    if any(row['workflow_id'] == workflow_id for row in workflow_rows(conversation)):
        return
    Message.objects.create(
        company_id=conversation.company_id,
        conversation=conversation,
        role=MessageRole.ASSISTANT,
        message_type=MessageType.CONTEXT,
        message=json.dumps({
            'kind': DOCUMENT_WORKFLOW_KIND,
            'filename': filename,
            'stored_path': stored_path,
            'document_id': document.pk,
            'workflow_id': workflow_id,
        }),
    )


def workflow_rows(conversation):
    """This chat's documents, newest first, one entry per workflow id."""
    seen, rows = set(), []
    for message in conversation.messages.filter(message_type=MessageType.CONTEXT).order_by('-created_at'):
        try:
            payload = json.loads(message.message or '')
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get('kind') != DOCUMENT_WORKFLOW_KIND:
            continue
        if payload.get('workflow_id') in seen:
            continue
        seen.add(payload['workflow_id'])
        rows.append(payload)
    return rows


def find_row(conversation, reference=''):
    """The chat document matching `reference` (document id or part of its name); the
    newest one when empty. Returns (row, None) or (None, message for the user)."""
    rows = workflow_rows(conversation)
    reference = str(reference).strip()
    if not rows:
        return None, None
    if not reference:
        return rows[0], None
    if reference.isdigit():
        matches = [row for row in rows if row['document_id'] == int(reference)]
    else:
        matches = [row for row in rows if reference.lower() in (row.get('filename') or '').lower()]
    if not matches:
        return None, f'No document matching "{reference}" has been summarized or saved in this chat.'
    if len(matches) > 1:
        options = '\n'.join(f'- id {row["document_id"]}: {row["filename"]}' for row in matches)
        return None, f'Several documents match "{reference}". Ask the user which one (by id):\n{options}'
    return matches[0], None


async def fetch_state(document_id):
    """The workflow's live state dict, or None if it cannot be read right now."""
    try:
        return await asyncio.wait_for(temporal.query_chat_state(document_id), timeout=STATE_TIMEOUT)
    except Exception:
        return None


async def pending_lines(conversation):
    """Lines for "what is pending" -- this chat's documents that are not yet in the
    knowledge base, plus any that are still processing or failed."""
    from document.models import Document, DocumentStatus

    rows = await sync_to_async(workflow_rows)(conversation)
    if not rows:
        return []

    states = await asyncio.gather(*(fetch_state(row['document_id']) for row in rows))
    statuses = dict(
        await sync_to_async(lambda: list(
            Document.all_objects.filter(pk__in=[r['document_id'] for r in rows]).values_list('pk', 'status'),
        ))(),
    )

    lines = []
    for row, state in zip(rows, states):
        name = row.get('filename') or f'Document {row["document_id"]}'
        db_status = statuses.get(row['document_id'])

        if state is None:
            # Live state unavailable: only the Document row is known.
            if db_status == DocumentStatus.COMPLETED:
                continue
            lines.append(
                f'- "{name}": pending -- add to knowledge base (live status unavailable; '
                f'the processing service is not answering)'
            )
            continue

        kb = state['kb_status']
        if kb == 'saved':
            continue
        if kb == 'saving':
            lines.append(f'- "{name}": adding to knowledge base is in progress')
        elif kb == 'failed':
            lines.append(f'- "{name}": adding to knowledge base FAILED ({state["kb_error"]}) -- can be retried')
        elif state['step'] == 'summarizing':
            lines.append(f'- "{name}": summary in progress; then pending -- add to knowledge base')
        else:
            note = 'summary ready' if state['summary'] else 'summary failed'
            lines.append(f'- "{name}": {note}; pending -- add to knowledge base')
    return lines
