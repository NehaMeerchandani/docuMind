"""Temporal activities for document processing.

Each activity is a thin wrapper around a DocumentService step (the same functions
DocumentService.process() runs for Celery/Kafka/admin), so there is no second copy of
the processing logic. They are plain `def`s run in a thread pool by the worker
(see the run_temporal_worker command), which is why every one is wrapped in
`_close_db_connections`: Django DB connections are per-thread, and without closing
stale ones a long-lived worker thread would hold on to a dead connection.
"""
import functools

import requests
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import default_storage
from django.db import close_old_connections
from temporalio import activity
from temporalio.exceptions import ApplicationError

from document.models import Document
from document.services.document_service import DocumentService
from document.services.parser_service import ParserService
from user.models import CustomUser


def _close_db_connections(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        close_old_connections()
        try:
            return func(*args, **kwargs)
        finally:
            close_old_connections()
    return wrapper


def _get_document(document_id):
    try:
        return Document.all_objects.get(pk=document_id)
    except ObjectDoesNotExist as exc:
        raise ApplicationError(f'Document {document_id} does not exist.', non_retryable=True) from exc


@activity.defn(name='parse_and_chunk')
@_close_db_connections
def parse_and_chunk(document_id: int, user_id: int | None) -> dict:
    """Parse + chunk in ONE activity so the (possibly huge) text never travels through
    Temporal's history -- only the chunk ids come back. Not embedded yet.
    """
    document = _get_document(document_id)
    user = CustomUser.objects.filter(pk=user_id).first() if user_id else None

    DocumentService.mark_processing(document, user=user)

    try:
        doc_type, text = DocumentService.parse(document)
        chunk_ids = DocumentService.create_chunks(document, text)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is not None and 400 <= status < 500 and status != 429:
            # 404 / 403 etc: retrying the same URL will not help.
            raise ApplicationError(f'The URL returned HTTP {status}.', non_retryable=True) from exc
        raise
    except ValueError as exc:
        # "No text found", unsupported/oversized file, blocked redirect: all deterministic.
        raise ApplicationError(str(exc), non_retryable=True) from exc

    return {'doc_type': doc_type, 'chunk_ids': chunk_ids}


@activity.defn(name='embed_batch')
@_close_db_connections
def embed_batch(chunk_ids: list[int]) -> int:
    """Embeds one batch. Heartbeats after every chunk, so a stuck/killed worker is
    noticed within the heartbeat timeout instead of the whole activity timeout.
    Re-running a batch is harmless (a chunk's Qdrant point id is its own id).
    """
    DocumentService.embed_chunks(
        chunk_ids,
        on_chunk_embedded=lambda done, total: activity.heartbeat(done),
    )
    return len(chunk_ids)


@activity.defn(name='complete_document')
@_close_db_connections
def complete_document(document_id: int, doc_type: str) -> None:
    DocumentService.mark_completed(_get_document(document_id), doc_type)


@activity.defn(name='fail_document')
@_close_db_connections
def fail_document(document_id: int, error_message: str) -> None:
    DocumentService.mark_failed(_get_document(document_id), error_message)


@activity.defn(name='cancel_document')
@_close_db_connections
def cancel_document(document_id: int) -> None:
    """User cancelled: drop the chunks made so far (and their vectors) and mark the
    document FAILED so it can be processed again later.
    """
    document = _get_document(document_id)
    document.chunks.all().delete()
    DocumentService.mark_failed(document, 'Cancelled by user')


@activity.defn(name='summarize_document_file')
@_close_db_connections
def summarize_document_file(document_id: int) -> dict:
    """Reads the chat-uploaded file, summarizes it with the LLM and returns the summary.

    The summary is returned to the workflow (kept in its state, so it lives in Temporal)
    and is deliberately NOT written to the Document table. Nothing is chunked or embedded
    here -- that only happens when the user asks to save the document to the knowledge base.
    """
    # Imported here: chat.services.tools pulls in LangChain, which only this activity needs.
    from chat.services.llm_client import get_chat_model
    from chat.services.tools import MAX_SUMMARY_CHARS, SUMMARY_SYSTEM_PROMPT

    document = _get_document(document_id)
    if not document.file:
        raise ApplicationError(f'Document {document_id} has no uploaded file to summarize.', non_retryable=True)

    try:
        with default_storage.open(document.file.name, 'rb') as stored_file:
            content = stored_file.read()
        _doc_type, text = ParserService.parse_file(document.title or document.file.name, content)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except OSError as exc:
        raise ApplicationError(f'The stored file could not be read: {exc}', non_retryable=True) from exc

    if not text.strip():
        raise ApplicationError('No extractable text was found in the file.', non_retryable=True)

    activity.heartbeat('summarizing')
    response = get_chat_model(temperature=0.3).invoke([
        {'role': 'system', 'content': SUMMARY_SYSTEM_PROMPT},
        {'role': 'user', 'content': text[:MAX_SUMMARY_CHARS]},
    ])
    return {
        'summary': response.content,
        'truncated': len(text) > MAX_SUMMARY_CHARS,
        'characters': len(text),
    }


ALL_ACTIVITIES = [
    parse_and_chunk, embed_batch, complete_document, fail_document, cancel_document,
    summarize_document_file,
]
