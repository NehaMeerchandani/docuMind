import json
import os

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langfuse import Langfuse

from chat.models import MessageRole, MessageType

FALLBACK_SYSTEM_PROMPT = (
    "You are a helpful assistant for a company's team. You have access to tools:\n\n"
    '- search_documents: call this for ANY question that could plausibly be answered by, '
    "or benefit from, the company's own uploaded documents — product details, internal "
    'processes, policies, pricing, people, or any specific fact about this company. This '
    "includes questions phrased generically (e.g. 'what is our policy on X', 'who is Y') "
    'even if a generic answer is also possible from general knowledge — always check the '
    "documents first in that case, since the user is almost always asking about *this* "
    "company's documents, not the general concept. If you use it and the results do not "
    "contain the answer, say you don't know rather than guessing.\n"
    '- summarize_session: use this only when the user explicitly asks for a summary of the '
    'conversation or session so far.\n\n'
    'Only skip search_documents when the question is unambiguously general knowledge with '
    "no plausible connection to this company (e.g. 'what is 2+2', 'what is the capital of "
    "France'). When in doubt, call search_documents rather than answering directly."
)


class PromptService:
    HISTORY_MESSAGE_LIMIT = 10

    _langfuse_client = None

    @classmethod
    def get_langfuse_client(cls):
        if cls._langfuse_client is None:
            # The langfuse SDK's own env-var convention is LANGFUSE_HOST; our .env uses
            # LANGFUSE_BASE_URL, so alias it for any SDK code (e.g. CallbackHandler) that
            # reads the environment directly instead of taking an explicit host= argument.
            os.environ.setdefault('LANGFUSE_HOST', os.getenv('LANGFUSE_BASE_URL', ''))

            cls._langfuse_client = Langfuse(
                public_key=os.getenv('LANGFUSE_PUBLIC_KEY'),
                secret_key=os.getenv('LANGFUSE_SECRET_KEY'),
                host=os.getenv('LANGFUSE_BASE_URL'),
            )
        return cls._langfuse_client

    @classmethod
    def get_system_prompt_and_config(cls):
        return cls.get_prompt_and_config('rag-system-prompt', fallback=FALLBACK_SYSTEM_PROMPT)

    @classmethod
    def get_prompt_and_config(cls, slug, fallback=''):
        """Fetch any Langfuse prompt by slug, e.g. a STEP node's `name` (agents/graph/
        builder.py) or the router's own prompt (agents/services/router_service.py).

        `fallback` is returned verbatim if Langfuse is unreachable or the slug doesn't
        exist yet there -- this keeps a brand-new STEP node usable immediately after
        being authored in the editor, before anyone has created its prompt in Langfuse.
        """
        client = cls.get_langfuse_client()
        prompt = client.get_prompt(
            slug,
            label='production',
            fallback=fallback,
            cache_ttl_seconds=60,
        )
        return prompt.compile(), (prompt.config or {})

    @classmethod
    def build_history(cls, conversation):
        """Turn this conversation's last HISTORY_MESSAGE_LIMIT TEXT messages into LangChain
        message objects, with the conversation's most recently attached file's table (if
        any) prepended as a SystemMessage.

        Only MessageType.TEXT rows are replayed as the actual back-and-forth. ERROR rows
        are deliberately excluded (we don't want a past failure re-entering the model's
        context). CONTEXT rows (currently only ever a chat file attachment's parsed table
        -- see ChatService._persist_attachment_context) are NOT part of that back-and-forth,
        but the latest one is still surfaced here as a leading SystemMessage, so that a
        later turn in the same session (e.g. "now chart that as a pie instead") still has
        the table available without the user re-attaching the file.

        Capped to the most recent messages rather than the full conversation, to keep the
        prompt small -- for anything older, the user is expected to say "use summary"
        instead (see ChatService), which uses conversation.summary in place of history.
        """
        recent_first = list(conversation.messages.filter(
            message_type=MessageType.TEXT,
        ).order_by('-created_at')[:cls.HISTORY_MESSAGE_LIMIT])
        history = list(reversed(recent_first))

        messages = [
            HumanMessage(content=message.message)
            if message.role == MessageRole.USER
            else AIMessage(content=message.message)
            for message in history
        ]

        # Appended at the END of history, deliberately NOT at index 0 -- the per-step
        # agent node (agents/graph/builder.py's call_agent) only injects that step's
        # OWN Langfuse-managed system prompt when `messages[0]` isn't already a system
        # message. A reminder placed at index 0 here would silently suppress the real
        # system prompt on every single turn of any conversation that ever had a file
        # attached, which would be a much worse bug than the one this is meant to fix.
        attachment_reminder = cls._build_attachment_reminder(conversation)
        if attachment_reminder:
            messages.append(attachment_reminder)

        return messages

    @classmethod
    def _build_attachment_reminder(cls, conversation):
        """Returns a SystemMessage summarizing the latest attached file's table (not its
        full row data a second time -- that already lives in the persisted CONTEXT row,
        which build_chart_from_file reads directly), or None if no file has been attached
        in this conversation. Just enough so the model knows a file exists and can decide
        to call build_chart_from_file again on a follow-up turn.
        """
        context_message = (
            conversation.messages.filter(message_type=MessageType.CONTEXT)
            .order_by('-created_at')
            .first()
        )
        if context_message is None or not context_message.message:
            return None

        try:
            payload = json.loads(context_message.message)
        except (TypeError, ValueError):
            return None

        if not isinstance(payload, dict) or payload.get('kind') != 'attachment_table':
            return None

        table = payload.get('table') or {}
        columns = table.get('columns') or []
        row_count = payload.get('row_count', len(table.get('rows') or []))

        return SystemMessage(content=(
            f'Earlier in this conversation, the user attached a file "{payload.get("filename", "")}" '
            f'with columns {columns} and {row_count} rows. If they now ask for a chart, graph, '
            'plot, or visualization (even without re-attaching the file), call build_chart_from_file.'
        ))

    @classmethod
    def build_summary_context(cls, conversation):
        """Turn this conversation's stored summary into a single LangChain message.

        Used instead of (never together with) build_history, when the user's question
        contains the "use summary" trigger phrase -- see ChatService.
        """
        if not conversation.summary:
            return [HumanMessage(
                content='(There is no saved summary for this conversation yet. '
                'Let the user know a summary needs to be created first.)',
            )]

        return [HumanMessage(content=f'Summary of this conversation so far:\n{conversation.summary}')]
