"""Chat tools for documents handled by the ChatDocumentWorkflow (Temporal).

  - summarize_with_temporal: start the workflow for an attached file; the summary is
    produced inside Temporal and kept there for the whole chat session.
  - get_document_summary: read a document's summary back (and its knowledge-base status).
  - save_to_knowledge_base: tell the workflow to chunk + embed + store the document
    (it runs the existing ProcessDocumentWorkflow as a child).
  - list_document_tasks: the table of the user's documents, with a button per action.

Any of them can act on a document by id or name from ANY chat (the task table's buttons
send e.g. "Save document 43 to the knowledge base"), as long as the user may manage it:
the uploader, or a superuser. Documents with no uploaded file (URL / pasted text) cannot
be summarised, so they go straight to the existing processing workflow.

The chat remembers only each document's name and workflow id (chat/services/
chat_documents.py). CSV/Excel files are chart-only and are refused here.
"""
import asyncio
import json
import os
import time

from asgiref.sync import sync_to_async
from langchain_core.tools import tool

from chat.services import chat_documents, task_service
from chat.services.document_process_tools import TEMPORAL_DOWN_NOTE, _can_manage, _label, _prepare_document
from chat.services.tools import TABULAR_EXTENSIONS, _find_attachment
from document.models import Document, DocumentStatus
from document.temporal import client as temporal
from user.models import CustomUser

SUMMARY_WAIT_SECONDS = 30
SUMMARY_POLL_SECONDS = 1.5
IN_PROGRESS_STEPS = ('pending', 'summarizing')


def _resolve_managed(company_id, user, reference):
    """A document of this company, by id or part of its title, that `user` may manage
    (the uploader or a superuser). Returns (document, None) or (None, message).
    A superuser can reach documents of every company (the admin's all-companies table)."""
    reference = str(reference).strip()
    documents = Document.objects.all() if user.is_superuser else Document.objects.filter(company_id=company_id)

    if reference.isdigit():
        document = documents.filter(pk=int(reference)).first()
        if document is None:
            return None, f'No document with id {reference} was found.'
        if not _can_manage(user, document):
            return None, f'Document {reference} was uploaded by someone else; only its uploader or an admin can act on it.'
        return document, None

    matches = [d for d in documents.filter(title__icontains=reference).order_by('-id')[:20] if _can_manage(user, d)]
    if not matches:
        return None, f'No document of yours matching "{reference}" was found.'
    if len(matches) > 1:
        options = '\n'.join(f'- id {d.pk}: {_label(d)} ({d.get_status_display()})' for d in matches[:10])
        return None, f'Several documents match "{reference}". Ask the user which one (by id):\n{options}'
    return matches[0], None


def _resolve_target(company_id, conversation, document_ref, stored_path):
    """Works out which document a tool call is about.
    Returns (document, user, filename, stored_path, error_message)."""
    user = CustomUser.objects.get(pk=conversation.user_id)

    if document_ref and str(document_ref).strip():
        row, _message = chat_documents.find_row(conversation, document_ref)
        if row is not None:
            document = Document.objects.filter(pk=row['document_id'], company_id=company_id).first()
            if document is not None:
                return document, user, row.get('filename') or _label(document), row.get('stored_path', ''), None

        # Not one of this chat's documents: any document the user may manage.
        document, error = _resolve_managed(company_id, user, document_ref)
        if error:
            return None, user, '', '', error
        return document, user, _label(document), document.file.name if document.file else '', None

    attachment = _find_attachment(conversation, stored_path)
    if attachment is None:
        row, _ = chat_documents.find_row(conversation)
        if row is None:
            return None, user, '', '', 'No file is attached in this conversation. Ask the user to attach one.'
        document = Document.objects.filter(pk=row['document_id'], company_id=company_id).first()
        if document is None:
            return None, user, '', '', f'"{row.get("filename")}" no longer exists as a document.'
        return document, user, row.get('filename') or _label(document), row.get('stored_path', ''), None

    filename = attachment.get('filename', '')
    if os.path.splitext(filename)[1].lower() in TABULAR_EXTENSIONS:
        return None, user, filename, '', (
            f'"{filename}" is a CSV/Excel file; those can only be charted, not summarized or '
            'saved to the knowledge base.'
        )

    document, user, error = _prepare_document(
        company_id, conversation, 0, '', '', '', attachment['stored_path'],
    )
    if error:
        return None, user, filename, '', error
    return document, user, filename, attachment['stored_path'], None


async def _start_direct_processing(document, user, conversation, filename):
    """For a document with no uploaded file (URL / pasted text): nothing to summarise, so
    run the existing processing workflow directly."""
    if document.status == DocumentStatus.COMPLETED:
        return f'"{filename}" is already saved in the knowledge base.'
    if document.status == DocumentStatus.PROCESSING:
        return f'"{filename}" (id {document.pk}) is already being processed. Ask for its status to see the progress.'
    try:
        workflow_id = await temporal.start_document_workflow(document, user, conversation.session_id)
    except Exception as exc:
        return (
            f'"{filename}" could not be processed: the processing service (Temporal) is unreachable '
            f'({type(exc).__name__}). Try again in a moment.'
        )
    return (
        f'"{filename}" (id {document.pk}) has no uploaded file to summarise, so it was sent straight '
        f'to the knowledge base (workflow {workflow_id}). Ask for its status any time.'
    )


def _summary_target(company_id, conversation, reference):
    """(document_id, name, workflow_id, error) for get_document_summary."""
    row, message = chat_documents.find_row(conversation, reference)
    if row is not None:
        return row['document_id'], row.get('filename') or f'Document {row["document_id"]}', row['workflow_id'], None
    if str(reference).strip():
        user = CustomUser.objects.get(pk=conversation.user_id)
        document, error = _resolve_managed(company_id, user, reference)
        if error:
            return None, '', '', error
        return document.pk, _label(document), temporal.chat_workflow_id_for(document.pk), None
    return None, '', '', message or (
        'No document has been processed in this chat yet. Ask the user whether to process an attached file.'
    )


async def _wait_for_summary(document_id):
    """Polls the workflow for up to SUMMARY_WAIT_SECONDS; returns the last state seen."""
    deadline = time.monotonic() + SUMMARY_WAIT_SECONDS
    state = None
    while time.monotonic() < deadline:
        state = await chat_documents.fetch_state(document_id)
        if state is not None and state['step'] not in IN_PROGRESS_STEPS:
            return state
        await asyncio.sleep(SUMMARY_POLL_SECONDS)
    return state


def _kb_note(state):
    kb = state['kb_status']
    if kb == 'saved':
        return 'It is saved in the knowledge base.'
    if kb == 'saving':
        return 'It is being added to the knowledge base now.'
    if kb == 'failed':
        return f'Adding it to the knowledge base failed ({state["kb_error"]}); it can be retried.'
    return 'It is NOT in the knowledge base yet -- the user can ask to save it there.'


def _format_summary(name, workflow_id, state):
    if state['summary']:
        cut = ' (the document was long, so only its first part was summarized)' if state['summary_truncated'] else ''
        return (
            f'Summary of "{name}"{cut} -- kept in this chat (workflow {workflow_id}). '
            f'{_kb_note(state)}\n\n{state["summary"]}'
        )
    if state['summary_error']:
        return f'The summary of "{name}" failed: {state["summary_error"]}. {_kb_note(state)}'
    return f'The summary of "{name}" is still being prepared (workflow {workflow_id}). Ask again in a moment.'


def make_summarize_with_temporal_tool(company_id, conversation):
    @tool
    async def summarize_with_temporal(stored_path: str = '', document: str = '') -> str:
        """PROCESS an attached document (PDF, DOCX, TXT or MD): it is read and summarized in
        a durable Temporal workflow, and the summary is kept for the whole chat session.
        Call this whenever the user says "process it", "process this document", "analyse
        it", "go through it" or "summarise it with Temporal" -- processing a chat document
        means summarizing it here. It does NOT put the document in the knowledge base (that
        only happens when the user asks to save it there -- save_to_knowledge_base).

        `stored_path`: the attached file's stored_path from the attachment note. `document`:
        instead, the id or name of an existing document (e.g. "process document 43" from
        the task table). Leave both empty to use the most recently attached file.
        """
        target, user, filename, stored, error = await sync_to_async(_resolve_target)(
            company_id, conversation, document, stored_path,
        )
        if error:
            return error
        if not target.file:
            return await _start_direct_processing(target, user, conversation, filename)
        document = target

        try:
            workflow_id = await temporal.start_chat_document_workflow(document, user, conversation.session_id)
        except Exception as exc:
            return (
                f'"{filename}" could not be summarized with Temporal: the processing service is '
                f'unreachable ({type(exc).__name__}). Try again in a moment, or use the instant '
                f'summary (summarize_document) instead.'
            )

        await sync_to_async(chat_documents.record_workflow)(conversation, document, workflow_id, filename, stored)

        state = await _wait_for_summary(document.pk)
        if state is None:
            return (
                f'Started summarizing "{filename}" (workflow {workflow_id}), but its progress cannot be '
                f'read right now. {TEMPORAL_DOWN_NOTE} Ask for the summary again in a moment.'
            )
        return _format_summary(filename, workflow_id, state)

    return summarize_with_temporal


def make_get_document_summary_tool(company_id, conversation):
    @tool
    async def get_document_summary(document: str = '') -> str:
        """Show the summary of a document that was processed (summarized) with Temporal,
        together with whether it is in the knowledge base. `document` is its name or id
        (any of the user's documents, e.g. "show the summary of document 43"); leave it
        empty for the most recent one in this chat. Use it when the user asks for the
        summary again, or what a document they uploaded was about.
        """
        document_id, name, workflow_id, error = await sync_to_async(_summary_target)(
            company_id, conversation, document,
        )
        if error:
            return error

        state = await chat_documents.fetch_state(document_id)
        if state is not None:
            return _format_summary(name, workflow_id, state)

        try:
            status_name, step = await asyncio.wait_for(temporal.describe_chat_workflow(document_id), timeout=8)
            return (
                f'"{name}": workflow {status_name.lower()}, last step "{step or "unknown"}". The summary '
                f'cannot be read right now because no worker is answering.'
            )
        except Exception:
            return (
                f'No summary is kept for "{name}": it has not been processed yet (say "process document '
                f'{document_id}"), or the processing service is unreachable.'
            )

    return get_document_summary


def make_save_to_knowledge_base_tool(company_id, conversation):
    @tool
    async def save_to_knowledge_base(document: str = '', stored_path: str = '') -> str:
        """Save a document to the knowledge base: it is split into chunks, embedded and
        stored so it can be searched later (the existing processing workflow). Call this
        when the user says "save it to the knowledge base", "save this document", "embed
        this", "add it to the knowledge base", "index it", "save document 43 ..." or "retry
        saving ..." (a failed or cancelled one is simply started again). Works for a document
        already processed in this chat and for a freshly attached file (it is then
        summarized first). Not for CSV/Excel files.

        `document`: the document's name or id from this chat; `stored_path`: an attached
        file's stored_path. Leave both empty to use the most recent document/attachment.
        Returns at once -- the work continues in the background.
        """
        target, user, filename, stored, error = await sync_to_async(_resolve_target)(
            company_id, conversation, document, stored_path,
        )
        if error:
            return error

        if target.status == DocumentStatus.COMPLETED:
            return f'"{filename}" is already saved in the knowledge base.'
        if target.status == DocumentStatus.PROCESSING:
            return f'"{filename}" (id {target.pk}) is already being processed. Ask for its status to see the progress.'
        if not target.file:
            return await _start_direct_processing(target, user, conversation, filename)

        try:
            workflow_id = await temporal.start_chat_document_workflow(
                target, user, conversation.session_id, save_to_knowledge_base=True,
            )
        except Exception as exc:
            return (
                f'"{filename}" could not be saved: the processing service (Temporal) is unreachable '
                f'({type(exc).__name__}). Try again in a moment.'
            )

        await sync_to_async(chat_documents.record_workflow)(conversation, target, workflow_id, filename, stored)

        return (
            f'Saving "{filename}" to the knowledge base has started (document id {target.pk}, workflow '
            f'{workflow_id}). It runs in the background; ask for its status any time.'
        )

    return save_to_knowledge_base


def make_list_document_tasks_tool(company_id, conversation):
    @tool
    async def list_document_tasks(scope: str = 'mine', company: str = '') -> str:
        """Show document tasks as a table with a button for every action (process, save to
        knowledge base, pause, resume, cancel, retry, view summary) and each document's
        workflow ids. Call it whenever the user asks what is running, pending, failed or
        completed, for their tasks/documents/workflows, or to restart/resume/cancel something
        and has not said which document.

        `scope`: "mine" (default) is the user's OWN documents in the selected company.
        Admins only: "company" is everyone's documents in the selected company ("show company
        documents", "all documents of this company"); "all" is every company's documents
        ("all companies"). `company`: an admin asking for a specific company by name ("show
        only Cygnus documents" -> company="Cygnus"); it implies scope "company".
        """
        user = await sync_to_async(CustomUser.objects.get)(pk=conversation.user_id)
        company_name = await sync_to_async(lambda: conversation.company.name)()
        payload, message = await task_service.build_task_table(
            company_id, user, scope, company_name, company,
        )
        if payload is None:
            return message
        return json.dumps({'table': payload, 'note_for_assistant': task_service.NOTE_FOR_ASSISTANT})

    return list_document_tasks
