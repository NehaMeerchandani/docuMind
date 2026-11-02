"""Django-side helpers for talking to Temporal: start a document workflow, ask it for
progress, pause/resume/cancel it. Used by the chat tools (and later the admin button).

Everything here is async (the Temporal Python client is), and runs inside the chat
views' event loop.
"""
import asyncio
import weakref
from datetime import timedelta

from django.conf import settings
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import (
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
    WorkflowIDConflictPolicy,
)

WORKFLOW_NAME = 'ProcessDocumentWorkflow'

# A query needs a live worker to answer. If none is running, give up quickly instead of
# leaving the chat hanging -- the caller falls back to describe() / the DB.
QUERY_TIMEOUT = timedelta(seconds=5)

DOCUMENT_ID_KEY = SearchAttributeKey.for_int('DocumentId')
SESSION_ID_KEY = SearchAttributeKey.for_keyword('SessionId')
UPLOADED_BY_KEY = SearchAttributeKey.for_keyword('UploadedBy')
PROCESSING_STEP_KEY = SearchAttributeKey.for_keyword('ProcessingStep')

# One cached Client per event loop. The admin chat's async views can run on different
# event loops (e.g. under runserver), and a Client is bound to the loop it was created
# on -- reusing it from another loop fails. A WeakKeyDictionary also drops an entry
# once its loop is garbage-collected.
_clients = weakref.WeakKeyDictionary()


async def get_client():
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = await Client.connect(settings.TEMPORAL_SERVER_URL, namespace=settings.TEMPORAL_NAMESPACE)
        _clients[loop] = client
    return client


def workflow_id_for(document_id):
    return f'process-document-{document_id}'


async def start_document_workflow(document, user, session_id):
    """Starts (or re-uses) the workflow for `document` and returns its workflow id.

    The id is derived from the document, and USE_EXISTING makes a second "process"
    request while one is running return the same workflow instead of starting another.
    Once a workflow has finished (completed/failed/cancelled) the id can be started again.
    """
    client = await get_client()
    workflow_id = workflow_id_for(document.pk)
    await client.start_workflow(
        WORKFLOW_NAME,
        {
            'document_id': document.pk,
            'uploaded_by_id': user.pk,
            'uploaded_by': user.email,
            'session_id': str(session_id),
            'company_id': document.company_id,
            'filename': document.title,
        },
        id=workflow_id,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        search_attributes=TypedSearchAttributes([
            SearchAttributePair(DOCUMENT_ID_KEY, document.pk),
            SearchAttributePair(SESSION_ID_KEY, str(session_id)),
            SearchAttributePair(UPLOADED_BY_KEY, user.email),
            SearchAttributePair(PROCESSING_STEP_KEY, 'pending'),
        ]),
    )
    return workflow_id


async def query_progress(document_id):
    """Live progress {step, embedded, total, paused} -- needs a worker to be running."""
    client = await get_client()
    handle = client.get_workflow_handle(workflow_id_for(document_id))
    return await handle.query('get_progress', rpc_timeout=QUERY_TIMEOUT)


async def describe_workflow(document_id):
    """(status_name, processing_step) straight from the Temporal server -- works even
    when no worker is running. Raises if the workflow does not exist."""
    client = await get_client()
    description = await client.get_workflow_handle(workflow_id_for(document_id)).describe(rpc_timeout=QUERY_TIMEOUT)
    step = description.typed_search_attributes.get(PROCESSING_STEP_KEY)
    return description.status.name, step


async def send_control(document_id, action):
    """pause/resume are Signals (fire and forget); cancel is an Update (it answers, and
    its validator can refuse). Returns a short human-readable result."""
    client = await get_client()
    handle = client.get_workflow_handle(workflow_id_for(document_id))

    if action in ('pause', 'resume'):
        await handle.signal(action)
        return f'Processing {action}d.' if action == 'pause' else 'Processing resumed.'

    try:
        return await handle.execute_update('cancel', rpc_timeout=QUERY_TIMEOUT)
    except WorkflowUpdateFailedError as exc:
        # The validator's reason ("already finished", "already requested").
        raise ValueError(str(exc.cause)) from exc
