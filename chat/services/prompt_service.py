import os

from langchain_core.messages import AIMessage, HumanMessage
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
        client = cls.get_langfuse_client()
        prompt = client.get_prompt(
            'rag-system-prompt',
            label='production',
            fallback=FALLBACK_SYSTEM_PROMPT,
            cache_ttl_seconds=60,
        )
        return prompt.compile(), (prompt.config or {})

    @classmethod
    def build_history(cls, conversation):
        """Turn this conversation's last HISTORY_MESSAGE_LIMIT TEXT messages into LangChain
        message objects.

        Only MessageType.TEXT rows are replayed into the agent's memory. ERROR rows are
        deliberately excluded (we don't want a past failure re-entering the model's context),
        and there's no CONTEXT/SYSTEM row type produced by the agent flow (tool results live
        inside the LangGraph run itself, not as persisted Message rows).

        Capped to the most recent messages rather than the full conversation, to keep the
        prompt small -- for anything older, the user is expected to say "use summary"
        instead (see ChatService), which uses conversation.summary in place of history.
        """
        recent_first = list(conversation.messages.filter(
            message_type=MessageType.TEXT,
        ).order_by('-created_at')[:cls.HISTORY_MESSAGE_LIMIT])
        history = list(reversed(recent_first))

        return [
            HumanMessage(content=message.message)
            if message.role == MessageRole.USER
            else AIMessage(content=message.message)
            for message in history
        ]

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
