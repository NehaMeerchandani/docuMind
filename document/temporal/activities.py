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
from django.db import close_old_connections
from temporalio import activity
from temporalio.exceptions import ApplicationError

from document.models import Document
from document.services.document_service import DocumentService
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


ALL_ACTIVITIES = [parse_and_chunk, embed_batch, complete_document, fail_document, cancel_document]
