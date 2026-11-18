"""Chat tools that start and manage Temporal document processing.

Three tools, all built per conversation (the conversation gives us the company, the
user, and the session id that is attached to the workflow):
  - process_document: create/find the Document and start its workflow.
  - get_document_status: live progress, with fallbacks if Temporal is unavailable.
  - control_document_processing: pause / resume / cancel.

Who may do what: anyone in the company can read a document's status; only the
uploader or a superuser can process or control it (the same rule as the admin buttons).
"""
import asyncio

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from langchain_core.tools import tool

from chat.services.tools import _find_attachment
from document.models import Document, DocumentStatus, DocumentType
from document.temporal import client as temporal
from user.models import CustomUser

CONTROL_ACTIONS = ('pause', 'resume', 'cancel')
TEMPORAL_DOWN_NOTE = (
    'The processing service (Temporal) could not be reached, so live progress is unavailable.'
)


def _can_manage(user, document):
    return bool(user.is_superuser or document.created_by_id == user.id)


def _label(document):
    return document.title or document.source_url or f'Document {document.pk}'


def _resolve_document(company_id, reference, any_company=False):
    """Finds one document of this company by id or (part of) its title (`any_company`:
    of any company, for admins). Returns (document, None), or (None, message) when none
    / several match."""
    reference = str(reference).strip()
    documents = Document.objects.all() if any_company else Document.objects.filter(company_id=company_id)

    if reference.isdigit():
        document = documents.filter(pk=int(reference)).first()
        if document is None:
            return None, f'No document with id {reference} was found in this company.'
        return document, None

    matches = list(documents.filter(title__icontains=reference).order_by('-id')[:10])
    if not matches:
        return None, f'No document matching "{reference}" was found in this company.'
    if len(matches) > 1:
        options = '\n'.join(f'- id {m.pk}: {_label(m)} ({m.get_status_display()})' for m in matches)
        return None, f'Several documents match "{reference}". Ask the user which one (by id):\n{options}'
    return matches[0], None


def _prepare_document(company_id, conversation, document_id, url, text, title, stored_path):
    """Sync part of process_document. Returns (document, user, error_message)."""
    user = CustomUser.objects.get(pk=conversation.user_id)

    if document_id:
        document = Document.objects.filter(pk=document_id, company_id=company_id).first()
        if document is None:
            return None, user, f'No document with id {document_id} was found in this company.'
    elif stored_path:
        attachment = _find_attachment(conversation, stored_path)
        if attachment is None:
            return None, user, 'That file is not attached to this conversation. Ask the user to attach it again.'
        document = Document.objects.filter(company_id=company_id, file=stored_path).first()
        if document is None:
            document = Document(
                company_id=company_id, created_by=user, file=stored_path,
                title=attachment.get('filename') or stored_path,
            )
    elif url:
        document = Document.objects.filter(company_id=company_id, source_url=url).first()
        if document is None:
            document = Document(company_id=company_id, created_by=user, source_url=url, title=title or url)
    elif text.strip():
        document = Document(
            company_id=company_id, created_by=user, content=text,
            title=title or text.strip()[:60], doc_type=DocumentType.TEXT,
        )
    else:
        return None, user, 'Nothing to process: give a document_id, a url, some text, or attach a file.'

    if not _can_manage(user, document) and document.pk:
        return None, user, 'Only the uploader or an admin can process this document.'

    if document.pk is None:
        try:
            # full_clean (not just save) so the SSRF validator on source_url actually runs.
            document.full_clean()
        except ValidationError as exc:
            return None, user, f'This document cannot be created: {"; ".join(exc.messages)}'
        document.save()

    return document, user, None


def make_process_document_tool(company_id, conversation):
    @tool
    async def process_document(
        document_id: int = 0, url: str = '', text: str = '', title: str = '', stored_path: str = '',
    ) -> str:
        """Index a document directly into the knowledge base (read, chunked, embedded) in
        the background. Use this ONLY for a web page/PDF `url`, pasted `text`, or an
        existing `document_id`. For a file the user attached in this chat, do NOT use this:
        "process it" means summarize_with_temporal, and "save it to the knowledge base" /
        "embed this" means save_to_knowledge_base.

        Pass exactly ONE source:
        - `document_id`: for a document that already exists (e.g. a failed/pending one).
        - `stored_path`: only if no other chat tool applies.
        - `url`: for a web page or PDF link.
        - `text`: for text the user pasted into the message (with an optional `title`).

        Returns immediately -- processing continues in the background. Tell the user the
        document id and that they can ask for its status at any time.
        """
        document, user, error = await sync_to_async(_prepare_document)(
            company_id, conversation, document_id, url.strip(), text, title, stored_path,
        )
        if error:
            return error

        if document.status == DocumentStatus.COMPLETED:
            chunks = await sync_to_async(document.chunks.count)()
            return (
                f'"{_label(document)}" (id {document.pk}) is already processed ({chunks} chunks). '
                'It can be searched now.'
            )
        if document.status == DocumentStatus.PROCESSING:
            return (
                f'"{_label(document)}" (id {document.pk}) is already being processed. '
                'Ask for its status to see the progress.'
            )

        try:
            workflow_id = await temporal.start_document_workflow(document, user, conversation.session_id)
        except Exception as exc:
            return (
                f'"{_label(document)}" (id {document.pk}) was saved, but processing could not be '
                f'started: the processing service (Temporal) is unreachable ({type(exc).__name__}). '
                'Try again in a moment.'
            )

        return (
            f'Started processing "{_label(document)}" (document id {document.pk}, '
            f'workflow {workflow_id}). It runs in the background; ask for its status any time.'
        )

    return process_document


def make_get_document_status_tool(company_id):
    @tool
    async def get_document_status(document: str) -> str:
        """Report how far a document's processing has got (e.g. "embedding 60/100"),
        whether it is paused, completed or failed. `document` is the document's id or
        its name/title. Call this when the user asks about the progress or status of a
        specific document. If several documents match the name, ask the user which one.
        """
        found, message = await sync_to_async(_resolve_document)(company_id, document)
        if found is None:
            return message

        return await _status_report(found)

    return get_document_status


async def _status_report(document):
    label = f'"{_label(document)}" (id {document.pk})'
    chunks = await sync_to_async(document.chunks.count)()

    # Layer 1: ask the running workflow itself -- exact, live progress.
    try:
        progress = await asyncio.wait_for(temporal.query_progress(document.pk), timeout=8)
        state = 'paused' if progress['paused'] else progress['step']
        line = f'{label}: {state}'
        if progress['step'] in ('embedding', 'done') and progress['total']:
            line += f' -- {progress["embedded"]}/{progress["total"]} chunks embedded'
        return line + _failure_suffix(document)
    except Exception:
        pass

    # Layer 2: ask the Temporal server (no worker needed) -- the workflow's state and
    # last known step, but not the exact count.
    try:
        status_name, step = await asyncio.wait_for(temporal.describe_workflow(document.pk), timeout=8)
        return (
            f'{label}: workflow {status_name.lower()}, last step "{step or "unknown"}". '
            f'Exact progress is unavailable right now (no worker is answering). '
            f'Saved status: {document.get_status_display()}, {chunks} chunks.' + _failure_suffix(document)
        )
    except Exception:
        pass

    # Layer 3: Temporal unreachable or never used for this document -- the database.
    step = f', step "{document.processing_step}"' if document.processing_step else ''
    return (
        f'{label}: {document.get_status_display()}{step}, {chunks} chunks saved. '
        f'({TEMPORAL_DOWN_NOTE if document.status == DocumentStatus.PROCESSING else "From saved status."})'
        + _failure_suffix(document)
    )


def _failure_suffix(document):
    if document.status == DocumentStatus.FAILED and document.error_message:
        return f' Failed: {document.error_message}'
    return ''


def make_control_document_processing_tool(company_id, conversation):
    @tool
    async def control_document_processing(document: str, action: str) -> str:
        """Pause, resume or cancel a document that is being processed. `document` is its
        id or name/title; `action` is exactly "pause", "resume" or "cancel". Pause/resume
        take effect before the next batch of chunks. Cancel stops processing, removes the
        chunks made so far and marks the document failed ("Cancelled by user"), so it can
        be processed again later.
        """
        action = action.strip().lower()
        if action not in CONTROL_ACTIONS:
            return 'action must be "pause", "resume" or "cancel".'

        user = await sync_to_async(CustomUser.objects.get)(pk=conversation.user_id)
        found, message = await sync_to_async(_resolve_document)(company_id, document, user.is_superuser)
        if found is None:
            return message

        if not _can_manage(user, found):
            return 'Only the uploader or an admin can control this document\'s processing.'

        try:
            result = await temporal.send_control(found.pk, action)
        except ValueError as exc:
            return f'Could not {action} "{_label(found)}": {exc}'
        except Exception:
            return (
                f'Could not {action} "{_label(found)}": there is no active processing for it '
                f'(it may have finished), or the processing service is unreachable.'
            )

        return f'"{_label(found)}" (id {found.pk}): {result}'

    return control_document_processing
