from typing import Annotated, TypedDict

from langchain_core.messages import SystemMessage
from langfuse.langchain import CallbackHandler
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from chat.services.llm_client import get_chat_model
from chat.services.prompt_service import PromptService
from chat.services.tools import make_search_documents_tool, make_summarize_session_tool


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


def get_langfuse_handler():
    PromptService.get_langfuse_client()
    return CallbackHandler()


def build_agent(company_id, conversation):
    system_prompt, config = PromptService.get_system_prompt_and_config()

    tools = [
        make_search_documents_tool(company_id),
        make_summarize_session_tool(conversation),
    ]

    llm = get_chat_model(streaming=True, temperature=config.get('temperature', 0.7))
    llm_with_tools = llm.bind_tools(tools)

    async def call_agent(state):
        messages = state['messages']
        if not messages or messages[0].type != 'system':
            messages = [SystemMessage(content=system_prompt), *messages]
        response = await llm_with_tools.ainvoke(messages)
        return {'messages': [response]}

    graph = StateGraph(AgentState)
    graph.add_node('agent', call_agent)
    graph.add_node('tools', ToolNode(tools))
    graph.add_edge(START, 'agent')
    graph.add_conditional_edges('agent', tools_condition)
    graph.add_edge('tools', 'agent')

    return graph.compile()
