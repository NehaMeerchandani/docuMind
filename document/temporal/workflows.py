"""ProcessDocumentWorkflow -- orchestration only.

Runs inside Temporal's workflow sandbox, so it must stay deterministic and must NOT
import Django or anything that touches the DB or network. It refers to activities by
name (see document/temporal/activities.py) and passes only small, JSON-friendly values
(ids, counts) between them -- never document text.
"""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

# How many chunks are embedded per activity call. Pause/cancel are checked between
# batches, so this is also how long a pause/cancel can take to kick in.
BATCH_SIZE = 10

PARSE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0, maximum_attempts=3)
EMBED_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=8,
)
DB_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5)


@workflow.defn(name='ProcessDocumentWorkflow')
class ProcessDocumentWorkflow:
    def __init__(self):
        self._step = 'pending'
        self._embedded = 0
        self._total = 0
        self._paused = False
        self._cancel_requested = False
        self._finishing = False

    @workflow.run
    async def run(self, params: dict) -> dict:
        """`params`: {document_id, uploaded_by_id, uploaded_by, session_id, company_id,
        filename}. Only document_id and uploaded_by_id are used here; the rest are
        there so they show up in the Temporal UI's Input tab.
        """
        document_id = params['document_id']

        try:
            self._set_step('parsing')
            prepared = await workflow.execute_activity(
                'parse_and_chunk',
                args=[document_id, params.get('uploaded_by_id')],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=PARSE_RETRY,
            )
            chunk_ids = prepared['chunk_ids']
            self._total = len(chunk_ids)

            self._set_step('embedding')
            for start in range(0, len(chunk_ids), BATCH_SIZE):
                # Durable: a paused workflow stays paused across worker crashes/restarts.
                await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)

                if self._cancel_requested:
                    await workflow.execute_activity(
                        'cancel_document',
                        document_id,
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=DB_RETRY,
                    )
                    self._set_step('cancelled')
                    return {'document_id': document_id, 'status': 'cancelled'}

                batch = chunk_ids[start:start + BATCH_SIZE]
                await workflow.execute_activity(
                    'embed_batch',
                    batch,
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=EMBED_RETRY,
                )
                self._embedded += len(batch)

            self._finishing = True
            await workflow.execute_activity(
                'complete_document',
                args=[document_id, prepared['doc_type']],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=DB_RETRY,
            )
            self._set_step('done')
            return {'document_id': document_id, 'status': 'completed', 'chunks': self._total}

        except ActivityError as exc:
            self._finishing = True
            self._set_step('failed')
            cause = exc.cause
            await workflow.execute_activity(
                'fail_document',
                args=[document_id, getattr(cause, 'message', None) or str(cause)],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=DB_RETRY,
            )
            raise

    @workflow.query
    def get_progress(self) -> dict:
        return {
            'step': self._step,
            'embedded': self._embedded,
            'total': self._total,
            'paused': self._paused,
        }

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.update
    def cancel(self) -> str:
        self._cancel_requested = True
        return 'Cancel requested: processing will stop before the next batch.'

    @cancel.validator
    def _validate_cancel(self) -> None:
        if self._finishing:
            raise ValueError('The document has already finished (or is finishing) processing.')
        if self._cancel_requested:
            raise ValueError('Cancel has already been requested.')

    def _set_step(self, step):
        self._step = step
        workflow.upsert_search_attributes({'ProcessingStep': [step]})
