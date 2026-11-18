"""The "my tasks" table shown in chat.

One row per document (the user's own by default), joining the Document table with what
Temporal knows: the chat workflow (summary) and the processing workflow (embedding).
Each row carries the buttons that make sense for its state; a button is just text the
chat sends as if the user had typed it ("Pause processing of document 43").

Temporal is read with one visibility-list call (no worker needed) plus live queries only
for workflows that are still running. If Temporal cannot be reached the table is built
from the database alone and says so.
"""
import asyncio
from datetime import datetime

from asgiref.sync import sync_to_async

from chat.services import chat_documents
from company.models import Company
from document.models import Document, DocumentStatus
from document.temporal import client as temporal

MAX_ROWS = 60
LIST_TIMEOUT = 8
PROGRESS_TIMEOUT = 3

COLUMNS = ['Document', 'Status', 'Actions']
NOTE_FOR_ASSISTANT = (
    'A table of these documents is already shown to the user, with buttons for every action. '
    'Do NOT repeat the rows. Reply with one or two short sentences (counts, and what the '
    'buttons do).'
)


# Row actions are sent as short codes; the browser turns each into a button whose text is
# sent as the user's message (static/admin/js/chat.js, TASK_ACTIONS), e.g. "save" ->
# "Save document 43 to the knowledge base". Keeping the table compact matters because
# the whole table JSON is also what the model reads.
ACTION_CODES = ('process', 'save', 'pause', 'resume', 'cancel', 'retry', 'summary')


def _actions(document, process, chat, chat_state, progress):
    summary_ready = bool(chat_state and chat_state.get('summary'))
    actions = []

    if process and process['status'] == 'RUNNING':
        return ['resume' if progress and progress.get('paused') else 'pause', 'cancel']

    if document.status == DocumentStatus.PROCESSING:
        return actions  # being processed outside Temporal (Celery/Kafka): nothing to control

    if summary_ready:
        actions.append('summary')
    if document.status == DocumentStatus.COMPLETED:
        return actions
    if chat and chat['status'] == 'RUNNING' and chat['step'] == 'summarizing':
        return actions

    if document.status == DocumentStatus.FAILED:
        actions.append('retry')
        return actions

    if document.file and not summary_ready:
        actions.append('process')
    actions.append('save')
    return actions


def _status(document, process, chat, chat_state, progress):
    """(label, kind) -- kind drives the badge colour: ok | run | warn | fail | idle."""
    if process and process['status'] == 'RUNNING':
        if progress:
            if progress.get('paused'):
                return 'Paused', 'warn'
            if progress.get('step') == 'embedding' and progress.get('total'):
                return f'Embedding {progress["embedded"]}/{progress["total"]}', 'run'
            return f'Processing ({progress.get("step")})', 'run'
        return f'Processing ({process["step"] or "running"})', 'run'
    if document.status == DocumentStatus.COMPLETED:
        return 'In knowledge base', 'ok'
    if document.status == DocumentStatus.FAILED:
        reason = (document.error_message or '').strip()
        return ('Failed: ' + reason[:60]) if reason else 'Failed', 'fail'
    if document.status == DocumentStatus.PROCESSING:
        return f'Processing ({document.processing_step or "running"})', 'run'
    if chat and chat['status'] == 'RUNNING' and chat['step'] == 'summarizing':
        return 'Summarising', 'run'
    return 'Pending', 'idle'


def _summary_label(chat, chat_state):
    if chat is None:
        return '-'
    if chat_state is not None:
        if chat_state.get('summary'):
            return 'Ready'
        if chat_state.get('summary_error'):
            return 'Failed'
        return 'In progress'
    if chat['step'] == 'summarizing':
        return 'In progress'
    if chat['step'] == 'summary_failed':
        return 'Failed'
    return 'Ready' if chat['step'] not in ('', 'pending', 'discarded') else '-'


async def _safe(coro, timeout):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except Exception:
        return None


async def _resolve_company(company_ref):
    """(company, None) for a company named (part of) `company_ref`, or (None, message)."""
    companies = await sync_to_async(lambda: list(Company.objects.filter(name__icontains=company_ref)[:10]))()
    if not companies:
        return None, f'No company matching "{company_ref}" was found.'
    exact = [c for c in companies if c.name.lower() == company_ref.lower()]
    picked = exact or companies
    if len(picked) > 1:
        return None, 'Several companies match "%s": %s. Ask which one.' % (company_ref, ', '.join(c.name for c in picked))
    return picked[0], None


async def build_task_table(company_id, user, scope='mine', company_name='', company_ref=''):
    """Returns (payload, plain_text): `payload` is the table dict, or None with a plain
    message when there is nothing to show.

    scope "mine"    -- your own documents in the selected company (everyone's default)
          "company" -- everyone's documents in a company (the selected one, or the one named
                       by `company_ref`)  } admins only; anyone else gets "mine" and a note
          "all"     -- everyone's documents in every company                      }
    """
    scope = (scope or 'mine').strip().lower()
    company_ref = (company_ref or '').strip()
    if company_ref:
        scope = 'company'
    if scope not in ('mine', 'company', 'all'):
        scope = 'mine'

    scope_note = ''
    if scope != 'mine' and not user.is_superuser:
        scope, company_ref = 'mine', ''
        scope_note = "Only admins can see other people's documents, so your own documents are shown."

    target_id, target_name = company_id, company_name
    if scope == 'company' and company_ref:
        company, error = await _resolve_company(company_ref)
        if company is None:
            return None, error
        target_id, target_name = company.pk, company.name

    def load():
        base = Document.objects.select_related('company', 'created_by')
        if scope == 'all':
            queryset = base
        elif scope == 'company':
            queryset = base.filter(company_id=target_id)
        else:
            queryset = base.filter(company_id=company_id, created_by=user)
        return list(queryset.order_by('-id')[:MAX_ROWS])

    documents = await sync_to_async(load)()
    if not documents:
        who = {
            'all': 'in any company',
            'company': f'in {target_name or "this company"}',
            'mine': 'of yours in this company',
        }[scope]
        hint = ' As an admin you can ask to see everyone\'s documents.' if user.is_superuser and scope == 'mine' else ''
        return None, f'There are no documents {who} yet. {scope_note}{hint}'.strip()

    workflows = await _safe(temporal.list_task_workflows(user.email if scope == 'mine' else None), LIST_TIMEOUT)
    temporal_ok = workflows is not None
    workflows = workflows or {}

    # Live detail only for workflows that are still open.
    async def progress_of(document):
        process = workflows.get(document.pk, {}).get('process')
        if process and process['status'] == 'RUNNING':
            return await _safe(temporal.query_progress(document.pk), PROGRESS_TIMEOUT)

    async def state_of(document):
        chat = workflows.get(document.pk, {}).get('chat')
        if chat and chat['status'] == 'RUNNING':
            return await chat_documents.fetch_state(document.pk)

    progresses = await asyncio.gather(*(progress_of(d) for d in documents))
    states = await asyncio.gather(*(state_of(d) for d in documents))

    rows, counts = [], {}
    for document, progress, chat_state in zip(documents, progresses, states):
        found = workflows.get(document.pk, {})
        process, chat = found.get('process'), found.get('chat')
        label, kind = _status(document, process, chat, chat_state, progress)
        counts[kind] = counts.get(kind, 0) + 1
        uploader = document.created_by.email.split('@')[0] if document.created_by_id else ''
        rows.append({
            'id': document.pk,
            'name': document.title or document.source_url or f'Document {document.pk}',
            'status': label,
            'status_kind': kind,
            'summary': _summary_label(chat, chat_state),
            'chat_workflow_id': chat['workflow_id'] if chat else '',
            'process_workflow_id': process['workflow_id'] if process else '',
            'actions': _actions(document, process, chat, chat_state, progress),
            # Tags that tell rows apart in the wider views (and feed the company filter).
            'company': document.company.name if scope == 'all' else '',
            'by': uploader if scope != 'mine' else '',
        })

    scope_label = {
        'mine': f'Your documents in {company_name or "this company"}',
        'company': f'All documents in {target_name or "this company"}',
        'all': 'All documents in all companies',
    }[scope]
    switches = None
    if user.is_superuser:
        middle = target_name if scope == 'company' else company_name
        switches = [
            {'label': 'Mine', 'text': 'Show my document tasks', 'active': scope == 'mine'},
            {
                'label': middle or 'This company',
                'text': f'Show all documents of company {middle}' if middle else 'Show all documents of this company',
                'active': scope == 'company',
            },
            {'label': 'All companies', 'text': 'Show document tasks of all companies', 'active': scope == 'all'},
        ]
    payload = {
        'title': 'Document tasks',
        'scope': scope,
        'scope_label': scope_label,
        'switches': switches,
        'generated_at': datetime.now().strftime('%d %b %Y, %H:%M'),
        'columns': COLUMNS,
        'rows': rows,
        'truncated': len(documents) == MAX_ROWS,
        'temporal_available': temporal_ok,
        'notice': scope_note or (
            '' if temporal_ok else
            'The processing service (Temporal) is not reachable, so workflow ids and live progress '
            'are missing. Statuses come from the database.'
        ),
        'counts': counts,
    }
    return payload, ''
