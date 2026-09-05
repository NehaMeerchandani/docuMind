import os

from langchain_openai import ChatOpenAI


def get_chat_model(streaming=False, temperature=0.7):
    return ChatOpenAI(
        base_url=os.getenv('LLM_BASE_URL'),
        api_key=os.getenv('LLM_API_KEY'),
        model=os.getenv('LLM_MODEL'),
        streaming=streaming,
        temperature=temperature,
    )
