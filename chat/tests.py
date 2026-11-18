import json
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.urls import reverse

from chat.models import Conversation, Message, MessageRole, MessageType
from chat.services import chat_documents
from chat.services.prompt_service import PromptService
from company.models import Company
from document.models import Document, DocumentStatus


class ChatDocumentsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Acme')
        self.user = get_user_model().objects.create_user(
            username='u', email='u@example.com', password='x', is_staff=True, is_superuser=True,
        )
        self.conversation = Conversation.objects.create(company=self.company, user=self.user)
        self.doc = Document.objects.create(company=self.company, created_by=self.user, title='report.pdf')

    def _attach(self):
        Message.objects.create(
            company=self.company, conversation=self.conversation, role=MessageRole.ASSISTANT,
            message_type=MessageType.CONTEXT,
            message=json.dumps({'kind': 'attachment_file', 'filename': 'report.pdf', 'stored_path': 'chat_uploads/report.pdf'}),
            media='chat_uploads/report.pdf',
        )

    def test_record_workflow_stores_name_and_id_once(self):
        wf = f'chat-document-{self.doc.pk}'
        chat_documents.record_workflow(self.conversation, self.doc, wf, 'report.pdf', 'chat_uploads/report.pdf')
        chat_documents.record_workflow(self.conversation, self.doc, wf, 'report.pdf', 'chat_uploads/report.pdf')

        rows = chat_documents.workflow_rows(self.conversation)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['filename'], rows[0]['workflow_id']), ('report.pdf', wf))
        self.assertEqual(chat_documents.find_row(self.conversation, 'REPORT')[0]['document_id'], self.doc.pk)
        self.assertEqual(chat_documents.find_row(self.conversation, str(self.doc.pk))[0]['workflow_id'], wf)
        self.assertIsNone(chat_documents.find_row(self.conversation, 'nope')[0])

    def test_workflow_row_does_not_hide_the_attachment_reminder(self):
        self._attach()
        chat_documents.record_workflow(
            self.conversation, self.doc, f'chat-document-{self.doc.pk}', 'report.pdf', 'chat_uploads/report.pdf',
        )
        history = PromptService.build_history(self.conversation)
        text = '\n'.join(message.content for message in history)
        self.assertIn('chat_uploads/report.pdf', text)  # attachment reminder
        self.assertIn(f'workflow chat-document-{self.doc.pk}', text)  # document reminder

    def test_pending_lines_lists_documents_not_yet_in_knowledge_base(self):
        chat_documents.record_workflow(self.conversation, self.doc, 'chat-document-1', 'report.pdf', 'p')
        state = {'step': 'summary_ready', 'summary': 's', 'kb_status': 'not_saved', 'kb_error': ''}

        with mock.patch.object(chat_documents, 'fetch_state', mock.AsyncMock(return_value=state)):
            lines = async_to_sync(chat_documents.pending_lines)(self.conversation)
        self.assertEqual(len(lines), 1)
        self.assertIn('summary ready; pending -- add to knowledge base', lines[0])

        with mock.patch.object(chat_documents, 'fetch_state', mock.AsyncMock(return_value={**state, 'kb_status': 'saved'})):
            self.assertEqual(async_to_sync(chat_documents.pending_lines)(self.conversation), [])

    def test_pending_lines_falls_back_to_the_database_when_temporal_is_down(self):
        chat_documents.record_workflow(self.conversation, self.doc, 'chat-document-1', 'report.pdf', 'p')
        with mock.patch.object(chat_documents, 'fetch_state', mock.AsyncMock(return_value=None)):
            lines = async_to_sync(chat_documents.pending_lines)(self.conversation)
        self.assertIn('live status unavailable', lines[0])

        Document.objects.filter(pk=self.doc.pk).update(status=DocumentStatus.COMPLETED)
        with mock.patch.object(chat_documents, 'fetch_state', mock.AsyncMock(return_value=None)):
            self.assertEqual(async_to_sync(chat_documents.pending_lines)(self.conversation), [])


class MediaAdminTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Acme')
        self.user = get_user_model().objects.create_user(
            username='admin', email='admin@example.com', password='x', is_staff=True, is_superuser=True,
        )
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(company=self.company, user=self.user)
        self.stored = default_storage.save('chat_uploads/_test_media.txt', ContentFile(b'hello'))
        self.addCleanup(default_storage.delete, self.stored)
        Message.objects.create(
            company=self.company, conversation=self.conversation, role=MessageRole.ASSISTANT,
            message_type=MessageType.CONTEXT, message='{}', media=self.stored,
        )

    def test_file_is_served_only_to_admins_and_traversal_is_refused(self):
        response = self.client.get(reverse('admin:chat_media_file', kwargs={'name': self.stored}))
        self.assertEqual(b''.join(response.streaming_content), b'hello')

        bad = self.client.get(reverse('admin:chat_media_file', kwargs={'name': '../manage.py'}))
        self.assertEqual(bad.status_code, 404)

        self.client.logout()
        anonymous = self.client.get(reverse('admin:chat_media_file', kwargs={'name': self.stored}))
        self.assertEqual(anonymous.status_code, 302)

    def test_message_admin_filters_by_media_and_links_the_file(self):
        Message.objects.create(
            company=self.company, conversation=self.conversation, role=MessageRole.USER, message='hi',
        )
        url = reverse('admin:chat_message_changelist')
        with_media = self.client.get(url, {'has_media': 'yes'})
        self.assertEqual(with_media.context['cl'].result_count, 1)
        self.assertContains(with_media, self.stored)
        self.assertEqual(self.client.get(url).context['cl'].result_count, 2)


class ChatDocumentToolsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Acme')
        self.user = get_user_model().objects.create_user(username='u', email='u@example.com', password='x')
        self.conversation = Conversation.objects.create(company=self.company, user=self.user)
        self.stored = default_storage.save('chat_uploads/_tool_test.pdf', ContentFile(b'%PDF'))
        self.addCleanup(default_storage.delete, self.stored)

    def _attach(self, filename, stored):
        Message.objects.create(
            company=self.company, conversation=self.conversation, role=MessageRole.ASSISTANT,
            message_type=MessageType.CONTEXT,
            message=json.dumps({'kind': 'attachment_file', 'filename': filename, 'stored_path': stored}),
        )

    def test_summarize_with_temporal_returns_summary_and_records_workflow(self):
        from chat.services.chat_document_tools import make_summarize_with_temporal_tool
        self._attach('report.pdf', self.stored)
        state = {
            'step': 'summary_ready', 'summary': 'A short summary.', 'summary_truncated': False,
            'summary_error': '', 'kb_status': 'not_saved', 'kb_error': '', 'filename': 'report.pdf',
        }
        with mock.patch('chat.services.chat_document_tools.temporal.start_chat_document_workflow',
                        mock.AsyncMock(return_value='chat-document-5')) as start, \
             mock.patch.object(chat_documents, 'fetch_state', mock.AsyncMock(return_value=state)):
            result = async_to_sync(make_summarize_with_temporal_tool(self.company.id, self.conversation).ainvoke)({})

        self.assertIn('A short summary.', result)
        self.assertIn('NOT in the knowledge base', result)
        self.assertFalse(start.call_args.kwargs.get('save_to_knowledge_base'))
        document = Document.objects.get(file=self.stored)
        self.assertEqual(document.status, DocumentStatus.PENDING)
        self.assertEqual(chat_documents.workflow_rows(self.conversation)[0]['workflow_id'], 'chat-document-5')

    def test_save_to_knowledge_base_sends_the_save_signal(self):
        from chat.services.chat_document_tools import make_save_to_knowledge_base_tool
        self._attach('report.pdf', self.stored)
        with mock.patch('chat.services.chat_document_tools.temporal.start_chat_document_workflow',
                        mock.AsyncMock(return_value='chat-document-5')) as start:
            result = async_to_sync(make_save_to_knowledge_base_tool(self.company.id, self.conversation).ainvoke)({})
        self.assertIn('has started', result)
        self.assertTrue(start.call_args.kwargs['save_to_knowledge_base'])

    def test_csv_is_refused(self):
        from chat.services.chat_document_tools import make_save_to_knowledge_base_tool
        self._attach('data.csv', 'chat_uploads/data.csv')
        with mock.patch('chat.services.chat_document_tools.temporal.start_chat_document_workflow') as start:
            result = async_to_sync(make_save_to_knowledge_base_tool(self.company.id, self.conversation).ainvoke)({})
        self.assertIn('only be charted', result)
        start.assert_not_called()

    def test_list_document_process_includes_pending_section(self):
        from chat.services.tools import make_list_document_process_tool
        document = Document.objects.create(company=self.company, created_by=self.user, title='report.pdf')
        chat_documents.record_workflow(self.conversation, document, 'chat-document-1', 'report.pdf', 'p')
        state = {'step': 'summary_ready', 'summary': 's', 'kb_status': 'not_saved', 'kb_error': ''}
        with mock.patch.object(chat_documents, 'fetch_state', mock.AsyncMock(return_value=state)):
            result = async_to_sync(make_list_document_process_tool(self.company.id, self.conversation).ainvoke)({})
        self.assertIn('Pending in this chat', result)
        self.assertIn('pending -- add to knowledge base', result)


class TaskTableTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Acme')
        User = get_user_model()
        self.me = User.objects.create_user(username='me', email='me@example.com', password='x')
        self.other = User.objects.create_user(username='ot', email='other@example.com', password='x')
        self.admin = User.objects.create_user(username='ad', email='admin@example.com', password='x', is_superuser=True)

    def _doc(self, user, title, status=DocumentStatus.PENDING, with_file=True):
        return Document.objects.create(
            company=self.company, created_by=user, title=title, status=status,
            file='chat_uploads/x.pdf' if with_file else '',
        )

    def _build(self, user, scope='mine', workflows=None, progress=None, state=None):
        from chat.services import task_service
        with mock.patch.object(task_service.temporal, 'list_task_workflows', mock.AsyncMock(return_value=workflows)), \
             mock.patch.object(task_service.temporal, 'query_progress', mock.AsyncMock(return_value=progress)), \
             mock.patch.object(chat_documents, 'fetch_state', mock.AsyncMock(return_value=state)):
            return async_to_sync(task_service.build_task_table)(self.company.id, user, scope, 'Acme')

    def test_only_own_documents_by_default(self):
        mine, theirs = self._doc(self.me, 'mine.pdf'), self._doc(self.other, 'theirs.pdf')
        payload, _ = self._build(self.me, workflows={})
        self.assertEqual([row['id'] for row in payload['rows']], [mine.pk])
        self.assertEqual(payload['scope'], 'mine')
        self.assertIsNone(payload['switches'])  # not an admin

    def test_normal_user_cannot_widen_the_scope(self):
        self._doc(self.other, 'theirs.pdf')
        mine = self._doc(self.me, 'mine.pdf')
        payload, _ = self._build(self.me, scope='all', workflows={})
        self.assertEqual([row['id'] for row in payload['rows']], [mine.pk])
        self.assertIn('Only admins', payload['notice'])

    def test_admin_can_see_everything_and_switch_back(self):
        self._doc(self.me, 'a.pdf'), self._doc(self.other, 'b.pdf')
        payload, _ = self._build(self.admin, scope='all', workflows={})
        self.assertEqual(len(payload['rows']), 2)
        self.assertEqual([o['label'] for o in payload['switches']], ['Mine', 'Acme', 'All companies'])
        self.assertTrue(payload['switches'][2]['active'])

    def test_empty_table_returns_a_plain_message(self):
        payload, message = self._build(self.me, workflows={})
        self.assertIsNone(payload)
        self.assertIn('no documents', message)

    def test_buttons_follow_the_state(self):
        pending = self._doc(self.me, 'pending.pdf')
        failed = self._doc(self.me, 'failed.pdf', DocumentStatus.FAILED)
        done = self._doc(self.me, 'done.pdf', DocumentStatus.COMPLETED)
        running = self._doc(self.me, 'running.pdf', DocumentStatus.PROCESSING)
        workflows = {running.pk: {'process': {'workflow_id': f'process-document-{running.pk}', 'status': 'RUNNING', 'step': 'embedding'}}}
        payload, _ = self._build(self.me, workflows=workflows, progress={'step': 'embedding', 'embedded': 10, 'total': 40, 'paused': False})
        rows = {row['id']: row for row in payload['rows']}

        labels = lambda doc: rows[doc.pk]['actions']
        self.assertEqual(labels(pending), ['process', 'save'])
        self.assertEqual(labels(failed), ['retry'])
        self.assertEqual(labels(done), [])
        self.assertEqual(labels(running), ['pause', 'cancel'])
        self.assertEqual(rows[running.pk]['status'], 'Embedding 10/40')
        self.assertEqual(rows[running.pk]['process_workflow_id'], f'process-document-{running.pk}')

    def test_paused_workflow_offers_resume(self):
        doc = self._doc(self.me, 'p.pdf', DocumentStatus.PROCESSING)
        workflows = {doc.pk: {'process': {'workflow_id': f'process-document-{doc.pk}', 'status': 'RUNNING', 'step': 'embedding'}}}
        payload, _ = self._build(self.me, workflows=workflows, progress={'step': 'embedding', 'embedded': 1, 'total': 5, 'paused': True})
        row = payload['rows'][0]
        self.assertEqual(row['status'], 'Paused')
        self.assertEqual(row['actions'], ['resume', 'cancel'])

    def test_summary_ready_offers_view_summary_and_hides_process(self):
        doc = self._doc(self.me, 'r.pdf')
        workflows = {doc.pk: {'chat': {'workflow_id': f'chat-document-{doc.pk}', 'status': 'RUNNING', 'step': 'summary_ready'}}}
        state = {'summary': 'text', 'summary_error': '', 'kb_status': 'not_saved'}
        row = self._build(self.me, workflows=workflows, state=state)[0]['rows'][0]
        self.assertEqual(row['summary'], 'Ready')
        self.assertEqual(row['actions'], ['summary', 'save'])
        self.assertEqual(row['chat_workflow_id'], f'chat-document-{doc.pk}')

    def test_temporal_down_falls_back_to_the_database(self):
        self._doc(self.me, 'x.pdf')
        payload, _ = self._build(self.me, workflows=None)
        self.assertFalse(payload['temporal_available'])
        self.assertIn('not reachable', payload['notice'])
        self.assertEqual(payload['rows'][0]['status'], 'Pending')


class ChatWidgetTests(TestCase):
    def test_task_table_json_becomes_a_widget_without_the_model_note(self):
        from chat.services.chat_service import ChatService
        raw = json.dumps({'table': {'rows': []}, 'note_for_assistant': 'x'})
        widget_type, data, payload = ChatService._as_widget(raw)
        self.assertEqual(widget_type, 'task_table')
        self.assertEqual(payload, {'table': {'rows': []}})

    def test_chart_json_still_becomes_a_chart_widget_and_text_is_ignored(self):
        from chat.services.chat_service import ChatService
        self.assertEqual(ChatService._as_widget(json.dumps({'chart': {'a': 1}}))[0], 'chart')
        self.assertIsNone(ChatService._as_widget('plain text'))
        self.assertIsNone(ChatService._as_widget('[1, 2]'))

    def test_upload_buttons_depend_on_file_type(self):
        from chat.services.chat_service import ChatService
        pdf = ChatService._upload_action_buttons('a.pdf')
        self.assertEqual([b['label'] for b in pdf], ['Process it', 'Save to knowledge base'])
        self.assertEqual(pdf[0]['text'], 'Process this document')
        self.assertEqual([b['label'] for b in ChatService._upload_action_buttons('a.csv')], ['Create a chart'])


class ManageAnyDocumentToolsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Acme')
        User = get_user_model()
        self.me = User.objects.create_user(username='me', email='me@example.com', password='x')
        self.other = User.objects.create_user(username='ot', email='other@example.com', password='x')
        self.conversation = Conversation.objects.create(company=self.company, user=self.me)

    def _save(self, ref):
        from chat.services.chat_document_tools import make_save_to_knowledge_base_tool
        return async_to_sync(make_save_to_knowledge_base_tool(self.company.id, self.conversation).ainvoke)({'document': ref})

    def test_save_by_id_works_for_my_document_that_is_not_in_this_chat(self):
        doc = Document.objects.create(company=self.company, created_by=self.me, title='old.pdf', file='chat_uploads/old.pdf')
        with mock.patch('chat.services.chat_document_tools.temporal.start_chat_document_workflow',
                        mock.AsyncMock(return_value=f'chat-document-{doc.pk}')) as start:
            result = self._save(str(doc.pk))
        self.assertIn('has started', result)
        self.assertTrue(start.call_args.kwargs['save_to_knowledge_base'])
        # Remembered in this chat from now on.
        self.assertEqual(chat_documents.workflow_rows(self.conversation)[0]['document_id'], doc.pk)

    def test_someone_elses_document_is_refused(self):
        doc = Document.objects.create(company=self.company, created_by=self.other, title='theirs.pdf', file='chat_uploads/t.pdf')
        with mock.patch('chat.services.chat_document_tools.temporal.start_chat_document_workflow') as start:
            result = self._save(str(doc.pk))
        self.assertIn('uploaded by someone else', result)
        start.assert_not_called()

    def test_document_without_a_file_goes_straight_to_the_processing_workflow(self):
        doc = Document.objects.create(company=self.company, created_by=self.me, title='page', source_url='https://example.com/a')
        with mock.patch('chat.services.chat_document_tools.temporal.start_document_workflow',
                        mock.AsyncMock(return_value=f'process-document-{doc.pk}')) as direct, \
             mock.patch('chat.services.chat_document_tools.temporal.start_chat_document_workflow') as chat_start:
            result = self._save(str(doc.pk))
        direct.assert_awaited_once()
        chat_start.assert_not_called()
        self.assertIn('straight', result)

    def test_process_by_id_uses_the_summarize_workflow(self):
        from chat.services.chat_document_tools import make_summarize_with_temporal_tool
        doc = Document.objects.create(company=self.company, created_by=self.me, title='old.pdf', file='chat_uploads/old.pdf')
        state = {'step': 'summary_ready', 'summary': 'S', 'summary_truncated': False, 'summary_error': '', 'kb_status': 'not_saved', 'kb_error': ''}
        with mock.patch('chat.services.chat_document_tools.temporal.start_chat_document_workflow',
                        mock.AsyncMock(return_value=f'chat-document-{doc.pk}')) as start, \
             mock.patch.object(chat_documents, 'fetch_state', mock.AsyncMock(return_value=state)):
            result = async_to_sync(make_summarize_with_temporal_tool(self.company.id, self.conversation).ainvoke)(
                {'document': str(doc.pk)},
            )
        self.assertIn('S', result)
        self.assertFalse(start.call_args.kwargs.get('save_to_knowledge_base'))

    def test_summary_by_id_for_a_document_outside_this_chat(self):
        from chat.services.chat_document_tools import make_get_document_summary_tool
        doc = Document.objects.create(company=self.company, created_by=self.me, title='old.pdf', file='chat_uploads/old.pdf')
        state = {'step': 'summary_ready', 'summary': 'Kept summary', 'summary_truncated': False, 'summary_error': '', 'kb_status': 'saved', 'kb_error': ''}
        with mock.patch.object(chat_documents, 'fetch_state', mock.AsyncMock(return_value=state)):
            result = async_to_sync(make_get_document_summary_tool(self.company.id, self.conversation).ainvoke)({'document': str(doc.pk)})
        self.assertIn('Kept summary', result)


class ChartNaNTests(TestCase):
    CSV = b"Name,Score,Hours\nAnu,90,5\nBen,,7\nCara,75,\n,60,3\n"

    def _table(self):
        import io
        from chat.services.table_parser_service import TableParserService
        return TableParserService.parse(io.BytesIO(self.CSV), 'Teacher.csv')

    def test_empty_csv_cells_become_none_not_nan(self):
        rows = self._table()['rows']
        self.assertIsNone(rows[1]['Score'])
        self.assertIsNone(rows[2]['Hours'])
        self.assertIsNone(rows[3]['Name'])
        json.dumps(rows, allow_nan=False)  # raises on NaN / Infinity

    def test_chart_with_gaps_is_valid_json_the_browser_can_read(self):
        from chat.services.chart_service import ChartService
        chart = ChartService.build_chart(self._table())
        data = {d['label']: d['data'] for d in chart['chart']['datasets']}
        self.assertEqual(data['Score'], [90.0, None, 75.0, 60.0])
        self.assertEqual(chart['chart']['labels'][3], '4')  # unnamed row falls back to its position
        json.dumps(chart, allow_nan=False)

    def test_pasted_data_with_gaps_is_clean_too(self):
        from chat.services.table_parser_service import TableParserService
        table = TableParserService.parse_text('Month,Revenue\nJan,100\nFeb,\nMar,300')
        json.dumps(table, allow_nan=False)

    def test_widget_payload_never_carries_nan(self):
        from chat.services.chat_service import ChatService
        raw = '{"chart": {"labels": ["a"], "datasets": [{"label": "x", "data": [NaN, Infinity, 3]}]}}'
        _type, data, _payload = ChatService._as_widget(raw)
        self.assertEqual(data['datasets'][0]['data'], [None, None, 3])
        json.dumps(data, allow_nan=False)


class AllCompaniesTests(TestCase):
    def setUp(self):
        self.kind = Company.objects.create(name='KindLife')
        self.cygnus = Company.objects.create(name='CygnusAlpha')
        User = get_user_model()
        self.admin = User.objects.create_user(username='ad', email='admin@example.com', password='x', is_superuser=True)
        self.neha = User.objects.create_user(username='ne', email='neha@example.com', password='x')
        self.admin_doc = Document.objects.create(company=self.kind, created_by=self.admin, title='admin.pdf', file='a.pdf')
        self.neha_doc = Document.objects.create(company=self.cygnus, created_by=self.neha, title='neha.pdf', file='n.pdf')

    def _table(self, user, scope, company, whole=False):
        from chat.services import task_service
        with mock.patch.object(task_service.temporal, 'list_task_workflows', mock.AsyncMock(return_value={})):
            result = async_to_sync(task_service.build_task_table)(company.id, user, scope, company.name)
        return result if whole else result[0]

    def test_admin_all_scope_spans_companies_and_tags_each_row(self):
        payload = self._table(self.admin, 'all', self.kind)
        rows = {row['id']: row for row in payload['rows']}
        self.assertEqual(set(rows), {self.admin_doc.pk, self.neha_doc.pk})
        self.assertEqual(rows[self.neha_doc.pk]['company'], 'CygnusAlpha')
        self.assertEqual(payload['scope_label'], 'All documents in all companies')
        self.assertEqual([o['active'] for o in payload['switches']], [False, False, True])

    def test_admin_mine_scope_stays_in_the_selected_company(self):
        payload = self._table(self.admin, 'mine', self.kind)
        self.assertEqual([r['id'] for r in payload['rows']], [self.admin_doc.pk])
        self.assertEqual(payload['rows'][0]['company'], '')
        self.assertEqual([o['active'] for o in payload['switches']], [True, False, False])

    def test_admin_can_act_on_another_companys_document_but_a_normal_user_cannot(self):
        from chat.services.chat_document_tools import _resolve_managed
        document, error = _resolve_managed(self.kind.id, self.admin, str(self.neha_doc.pk))
        self.assertEqual(document, self.neha_doc)
        self.assertIsNone(error)

        outsider = get_user_model().objects.create_user(username='x', email='x@example.com', password='x')
        document, error = _resolve_managed(self.cygnus.id, outsider, str(self.neha_doc.pk))
        self.assertIsNone(document)
        self.assertIn('someone else', error)

    def test_pause_by_id_works_across_companies_for_an_admin(self):
        from chat.services.document_process_tools import make_control_document_processing_tool
        conversation = Conversation.objects.create(company=self.kind, user=self.admin)
        with mock.patch('chat.services.document_process_tools.temporal.send_control',
                        mock.AsyncMock(return_value='Processing paused.')) as control:
            result = async_to_sync(make_control_document_processing_tool(self.kind.id, conversation).ainvoke)(
                {'document': str(self.neha_doc.pk), 'action': 'pause'},
            )
        control.assert_awaited_once_with(self.neha_doc.pk, 'pause')
        self.assertIn('paused', result)


    def test_admin_with_no_documents_is_told_about_the_all_companies_view(self):
        payload, message = self._table(self.admin, 'mine', self.cygnus, whole=True)
        self.assertIsNone(payload)
        self.assertIn("everyone's documents", message)

    # --- the three views: mine / a company / all companies ---

    def test_company_scope_shows_everyones_documents_of_the_selected_company_only(self):
        colleague = get_user_model().objects.create_user(username='co', email='colleague@example.com', password='x')
        colleague_doc = Document.objects.create(company=self.kind, created_by=colleague, title='colleague.pdf', file='c.pdf')
        payload = self._table(self.admin, 'company', self.kind)
        rows = {row['id']: row for row in payload['rows']}
        self.assertEqual(set(rows), {self.admin_doc.pk, colleague_doc.pk})  # not Neha's (CygnusAlpha)
        self.assertEqual(payload['scope_label'], 'All documents in KindLife')
        self.assertEqual(rows[colleague_doc.pk]['by'], 'colleague')
        self.assertEqual(rows[colleague_doc.pk]['company'], '')  # one company: no company tag needed
        self.assertEqual([o['active'] for o in payload['switches']], [False, True, False])

    def test_a_company_can_be_named_even_if_another_is_selected(self):
        from chat.services import task_service
        with mock.patch.object(task_service.temporal, 'list_task_workflows', mock.AsyncMock(return_value={})):
            payload, _ = async_to_sync(task_service.build_task_table)(self.kind.id, self.admin, 'mine', 'KindLife', 'cygnus')
        self.assertEqual([row['id'] for row in payload['rows']], [self.neha_doc.pk])
        self.assertEqual(payload['scope'], 'company')
        self.assertEqual(payload['scope_label'], 'All documents in CygnusAlpha')
        self.assertEqual(payload['switches'][1]['label'], 'CygnusAlpha')

    def test_unknown_company_gets_a_clear_message(self):
        from chat.services import task_service
        payload, message = async_to_sync(task_service.build_task_table)(self.kind.id, self.admin, 'company', 'KindLife', 'nope')
        self.assertIsNone(payload)
        self.assertIn('No company matching "nope"', message)

    def test_normal_user_cannot_use_company_or_all_views(self):
        mine = Document.objects.create(company=self.kind, created_by=self.neha, title='n.pdf', file='n2.pdf')
        for scope in ('company', 'all'):
            payload = self._table(self.neha, scope, self.kind)
            self.assertEqual([row['id'] for row in payload['rows']], [mine.pk])
            self.assertIn('Only admins', payload['notice'])
        from chat.services import task_service
        payload, _ = async_to_sync(task_service.build_task_table)(self.kind.id, self.neha, 'mine', 'KindLife', 'cygnus')
        self.assertEqual([row['id'] for row in payload['rows']], [mine.pk])  # naming a company does not get around it

    def test_uploader_and_company_tags_only_appear_in_the_wider_views(self):
        mine = self._table(self.admin, 'mine', self.kind)['rows'][0]
        self.assertEqual((mine['by'], mine['company']), ('', ''))
        allrows = {r['id']: r for r in self._table(self.admin, 'all', self.kind)['rows']}
        self.assertEqual((allrows[self.neha_doc.pk]['by'], allrows[self.neha_doc.pk]['company']), ('neha', 'CygnusAlpha'))
