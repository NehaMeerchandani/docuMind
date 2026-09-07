from langfuse.langchain import CallbackHandler

from chat.services.prompt_service import PromptService


def get_langfuse_handler():
    PromptService.get_langfuse_client()
    return CallbackHandler()
