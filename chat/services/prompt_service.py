import os

from langchain_core.messages import AIMessage, HumanMessage
from langfuse import Langfuse

from chat.models import MessageSender, MessageType

FALLBACK_SYSTEM_PROMPT = (
    "You are a helpful assistant for a company's team. You have access to tools:\n\n"
    '- search_documents: use this when the question likely requires specific information '
    "from the company's own uploaded documents (e.g. product details, internal processes, "
    'facts from uploaded content). If you use it and the results do not contain the answer, '
    "say you don't know rather than guessing.\n"
    '- summarize_session: use this only when the user explicitly asks for a summary of the '
    'conversation or session so far.\n\n'
    'For general knowledge questions that clearly do not need the company documents, answer '
    'directly from your own knowledge without calling any tool.'
)


class PromptService:
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
        """Turn this conversation's saved TEXT messages into LangChain message objects.

        Only MessageType.TEXT rows are replayed into the agent's memory. ERROR rows are
        deliberately excluded (we don't want a past failure re-entering the model's context),
        and there's no CONTEXT/SYSTEM row type produced by the agent flow (tool results live
        inside the LangGraph run itself, not as persisted Message rows).
        """
        history = conversation.messages.filter(message_type=MessageType.TEXT).order_by('created_at')

        return [
            HumanMessage(content=message.content)
            if message.sender == MessageSender.USER
            else AIMessage(content=message.content)
            for message in history
        ]
