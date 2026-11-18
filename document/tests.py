"""Integration tests for ChatDocumentWorkflow against a throwaway local Temporal dev
server (the Temporal CLI, in memory). Activities are replaced by in-memory fakes with the
same names, so no database, LLM or Qdrant is needed. Skipped if the CLI is not installed.
"""
import os
import shutil
import unittest
import uuid
from types import SimpleNamespace

from unittest import mock

from django.conf import settings
from django.test import TestCase
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from document.temporal import client as temporal
from document.temporal.workflows import ChatDocumentWorkflow, ProcessDocumentWorkflow

CLI = shutil.which('temporal') or os.path.expanduser('~/.temporalio/bin/temporal')
QUEUE = settings.TEMPORAL_TASK_QUEUE  # the server is a private in-memory one


class FakeActivities:
    def __init__(self, summary_fails=False):
        self.calls = []
        self.summary_fails = summary_fails

    @activity.defn(name='summarize_document_file')
    async def summarize_document_file(self, document_id: int) -> dict:
        self.calls.append('summarize')
        if self.summary_fails:
            raise ApplicationError('No extractable text.', non_retryable=True)
        return {'summary': f'summary of {document_id}', 'truncated': False, 'characters': 10}

    @activity.defn(name='parse_and_chunk')
    async def parse_and_chunk(self, document_id: int, user_id) -> dict:
        self.calls.append('parse')
        return {'doc_type': 'pdf', 'chunk_ids': [1, 2, 3]}

    @activity.defn(name='embed_batch')
    async def embed_batch(self, chunk_ids: list) -> int:
        self.calls.append('embed')
        return len(chunk_ids)

    @activity.defn(name='complete_document')
    async def complete_document(self, document_id: int, doc_type: str) -> None:
        self.calls.append('complete')

    @activity.defn(name='fail_document')
    async def fail_document(self, document_id: int, error_message: str) -> None:
        self.calls.append('fail')

    @activity.defn(name='cancel_document')
    async def cancel_document(self, document_id: int) -> None:
        self.calls.append('cancel')

    def all(self):
        return [
            self.summarize_document_file, self.parse_and_chunk, self.embed_batch,
            self.complete_document, self.fail_document, self.cancel_document,
        ]


@unittest.skipUnless(os.path.exists(CLI), 'Temporal CLI not installed')
class ChatDocumentWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await WorkflowEnvironment.start_local(
            dev_server_existing_path=CLI,
            dev_server_extra_args=[
                '--search-attribute', 'DocumentId=Int', '--search-attribute', 'SessionId=Keyword',
                '--search-attribute', 'UploadedBy=Keyword', '--search-attribute', 'ProcessingStep=Keyword',
            ],
        )
        temporal._clients.clear()
        self.addCleanup(temporal._clients.clear)
        self.client = self.env.client
        # The helpers in client.py connect through settings; point them at the test client.
        import asyncio
        temporal._clients[asyncio.get_running_loop()] = self.client
        self.document = SimpleNamespace(pk=int(uuid.uuid4().int % 10**8), company_id=1, title='report.pdf')
        self.user = SimpleNamespace(pk=7, email='u@example.com')

    async def asyncTearDown(self):
        await self.env.shutdown()

    def _worker(self, fakes):
        return Worker(
            self.client, task_queue=QUEUE,
            workflows=[ChatDocumentWorkflow, ProcessDocumentWorkflow], activities=fakes.all(),
        )

    async def _wait_for(self, predicate, attempts=50):
        import asyncio
        for _ in range(attempts):
            state = await temporal.query_chat_state(self.document.pk)
            if predicate(state):
                return state
            await asyncio.sleep(0.1)
        self.fail(f'condition not reached, last state: {state}')

    async def test_summary_is_kept_in_workflow_and_nothing_is_embedded(self):
        fakes = FakeActivities()
        async with self._worker(fakes):
            workflow_id = await temporal.start_chat_document_workflow(self.document, self.user, 'sess-1')
            self.assertEqual(workflow_id, f'chat-document-{self.document.pk}')

            state = await self._wait_for(lambda s: s['step'] == 'summary_ready')
            self.assertEqual(state['summary'], f'summary of {self.document.pk}')
            self.assertEqual(state['kb_status'], 'not_saved')
            self.assertEqual(state['filename'], 'report.pdf')
            self.assertEqual(fakes.calls, ['summarize'])  # no parse/embed yet

            # Still open: a second start re-uses it instead of summarizing again.
            await temporal.start_chat_document_workflow(self.document, self.user, 'sess-1')
            status, step = await temporal.describe_chat_workflow(self.document.pk)
            self.assertEqual(status, WorkflowExecutionStatus.RUNNING.name)
            self.assertEqual(step, 'summary_ready')
            self.assertEqual(fakes.calls, ['summarize'])

    async def test_save_to_knowledge_base_runs_processing_as_child(self):
        fakes = FakeActivities()
        async with self._worker(fakes):
            await temporal.start_chat_document_workflow(self.document, self.user, 'sess-1')
            await self._wait_for(lambda s: s['step'] == 'summary_ready')

            await temporal.signal_chat_workflow(self.document.pk, 'save_to_knowledge_base')
            state = await self._wait_for(lambda s: s['kb_status'] == 'saved')

            self.assertEqual(state['summary'], f'summary of {self.document.pk}')  # still there
            self.assertEqual(fakes.calls, ['summarize', 'parse', 'embed', 'complete'])
            child = self.client.get_workflow_handle(temporal.workflow_id_for(self.document.pk))
            self.assertEqual((await child.describe()).status, WorkflowExecutionStatus.COMPLETED)

            # Saving again does nothing; the chat workflow stays open for later queries.
            await temporal.signal_chat_workflow(self.document.pk, 'save_to_knowledge_base')
            status, _ = await temporal.describe_chat_workflow(self.document.pk)
            self.assertEqual(status, WorkflowExecutionStatus.RUNNING.name)
            self.assertEqual(fakes.calls.count('parse'), 1)

    async def test_start_with_save_signal_summarizes_then_saves(self):
        fakes = FakeActivities()
        async with self._worker(fakes):
            await temporal.start_chat_document_workflow(
                self.document, self.user, 'sess-1', save_to_knowledge_base=True,
            )
            await self._wait_for(lambda s: s['kb_status'] == 'saved')
            self.assertEqual(fakes.calls, ['summarize', 'parse', 'embed', 'complete'])

    async def test_signal_reaches_an_already_running_workflow(self):
        fakes = FakeActivities()
        async with self._worker(fakes):
            await temporal.start_chat_document_workflow(self.document, self.user, 'sess-1')
            await self._wait_for(lambda s: s['step'] == 'summary_ready')
            await temporal.start_chat_document_workflow(
                self.document, self.user, 'sess-1', save_to_knowledge_base=True,
            )
            await self._wait_for(lambda s: s['kb_status'] == 'saved')
            self.assertEqual(fakes.calls.count('summarize'), 1)

    async def test_failed_summary_does_not_block_saving(self):
        fakes = FakeActivities(summary_fails=True)
        async with self._worker(fakes):
            await temporal.start_chat_document_workflow(self.document, self.user, 'sess-1')
            state = await self._wait_for(lambda s: s['step'] == 'summary_failed')
            self.assertEqual(state['summary'], '')
            self.assertIn('No extractable text', state['summary_error'])

            await temporal.signal_chat_workflow(self.document.pk, 'save_to_knowledge_base')
            await self._wait_for(lambda s: s['kb_status'] == 'saved')

    async def test_discard_ends_the_workflow(self):
        fakes = FakeActivities()
        async with self._worker(fakes):
            await temporal.start_chat_document_workflow(self.document, self.user, 'sess-1')
            await self._wait_for(lambda s: s['step'] == 'summary_ready')
            await temporal.signal_chat_workflow(self.document.pk, 'discard')
            handle = self.client.get_workflow_handle(temporal.chat_workflow_id_for(self.document.pk))
            result = await handle.result()
            self.assertEqual(result['status'], 'discarded')
            self.assertEqual(fakes.calls, ['summarize'])

    async def test_listing_tasks_by_uploader_returns_both_workflows(self):
        import asyncio
        fakes = FakeActivities()
        async with self._worker(fakes):
            await temporal.start_chat_document_workflow(self.document, self.user, 'sess-1', save_to_knowledge_base=True)
            await self._wait_for(lambda s: s['kb_status'] == 'saved')

            found = {}
            for _ in range(50):  # the visibility store is eventually consistent
                found = await temporal.list_task_workflows(self.user.email)
                if {'chat', 'process'} <= set(found.get(self.document.pk, {})):
                    break
                await asyncio.sleep(0.2)

            entry = found[self.document.pk]
            self.assertEqual(entry['chat']['workflow_id'], f'chat-document-{self.document.pk}')
            self.assertEqual(entry['chat']['status'], 'RUNNING')
            self.assertEqual(entry['process']['workflow_id'], f'process-document-{self.document.pk}')
            self.assertEqual(entry['process']['status'], 'COMPLETED')

            # Someone else's filter sees nothing of mine.
            self.assertEqual(await temporal.list_task_workflows('nobody@example.com'), {})


class NulCharacterTests(TestCase):
    """PostgreSQL cannot store NUL (0x00); a PDF whose text contained one failed with
    "A string literal cannot contain NUL (0x00) characters" when its chunks were saved."""

    def test_parser_drops_nul_characters(self):
        from document.services.parser_service import ParserService
        doc_type, text = ParserService.parse_file('notes.txt', b'before\x00after\x00')
        self.assertEqual(text, 'beforeafter')

    def test_chunks_with_nul_in_the_text_can_be_saved(self):
        from company.models import Company
        from document.models import Chunk, Document
        from document.services.document_service import DocumentService

        company = Company.objects.create(name='Acme')
        document = Document.objects.create(company=company, title='scan.pdf')
        with mock.patch('document.models.EmbeddingService.delete_points'):
            chunk_ids = DocumentService.create_chunks(document, 'Revenue grew\x00 in May.\x00 ' * 20)

        saved = Chunk.objects.filter(pk__in=chunk_ids)
        self.assertTrue(saved.exists())
        self.assertFalse(any('\x00' in chunk.content for chunk in saved))
