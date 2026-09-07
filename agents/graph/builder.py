from typing import Annotated, TypedDict

from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from agents.services.tool_registry import ToolRegistry
from chat.services.llm_client import get_chat_model
from chat.services.prompt_service import PromptService

START_NODE_TYPE = 'start'
STEP_NODE_TYPE = 'step'
END_NODE_TYPE = 'end'

# Edge condition strings authored in the visual editor (see WorkflowEditor.js).
CONDITION_ALWAYS = 'always'
CONDITION_TOOL_CALLED_PREFIX = 'tool_called:'
CONDITION_TOOL_NOT_CALLED_PREFIX = 'tool_not_called:'


class WorkflowValidationError(ValueError):
    """Raised when an Agent's workflow_json is structurally invalid -- missing a start
    node, a dangling edge, an unregistered tool id, etc. Caught by both the admin's
    save endpoint (agents/admin.py) and the graph builder itself, so a bad workflow is
    rejected at author-time rather than failing confusingly mid-chat."""


class WorkflowState(TypedDict):
    messages: Annotated[list, add_messages]
    # Which tools actually got called during each STEP node's own run so far, keyed by
    # that node's id, e.g. {"n2": ["search_documents"]}. Every step-agent/step-tools node
    # function reads the current dict, adds/extends its own key, and returns the whole
    # dict back -- there's no per-key merge reducer, so each writer is responsible for
    # preserving what earlier steps already recorded.
    step_tool_calls: dict


def _default_fallback_prompt(tool_ids):
    if not tool_ids:
        return 'You are a helpful assistant. Answer the user directly from your own knowledge.'
    return (
        f"You are a helpful assistant with access to these tools: {', '.join(tool_ids)}. "
        "Use whichever tool actually helps answer the user's question; if none of them "
        'are relevant, answer directly from your own knowledge instead.'
    )


class WorkflowGraphBuilder:
    """Compiles one Agent.workflow_json into a real, runnable LangGraph graph.

    Each STEP node in the workflow becomes its own small ReAct loop (an "agent" node
    bound to just that step's assigned tools, plus a "tools" node that executes them),
    fetching its own system prompt from Langfuse using the step's `name` as the prompt
    slug. Once a step's agent produces a response with no further tool calls, a router
    function evaluates that step's outgoing edges (in the order authored) against which
    tools were actually called during the step's run, and moves on to whichever step
    (or END) the first matching edge points to. A step with no matching/no outgoing
    edges ends the graph there, which is what lets a STEP double as an implicit end
    node, per the workflow's design.
    """

    @classmethod
    def validate(cls, workflow_json):
        """Raises WorkflowValidationError if workflow_json is structurally invalid.
        Used by both the editor's save endpoint (agents/admin.py) and build() below, so
        there's exactly one definition of "valid" -- a workflow that fails here could
        never have run in chat either."""
        cls._parse_and_validate(workflow_json)

    @classmethod
    def build(cls, agent, company_id, conversation):
        nodes_by_id, edges = cls._parse_and_validate(agent.workflow_json)

        step_nodes = {
            node_id: node for node_id, node in nodes_by_id.items() if node['type'] == STEP_NODE_TYPE
        }
        edges_by_source = {}
        for edge in edges:
            edges_by_source.setdefault(edge['source'], []).append(edge)

        graph = StateGraph(WorkflowState)

        for step_id, step in step_nodes.items():
            agent_node_name = cls._agent_node_name(step_id)
            tools_node_name = cls._tools_node_name(step_id)

            call_agent, call_tools, has_tools = cls._build_step_callables(
                step, company_id, conversation,
            )
            graph.add_node(agent_node_name, call_agent)

            outgoing = edges_by_source.get(step_id, [])
            router = cls._build_step_router(step_id, tools_node_name, outgoing, nodes_by_id, has_tools)
            graph.add_conditional_edges(agent_node_name, router)

            if has_tools:
                graph.add_node(tools_node_name, call_tools)
                graph.add_edge(tools_node_name, agent_node_name)

        start_node_id = cls._find_single(nodes_by_id, START_NODE_TYPE, 'start')
        start_edges = edges_by_source.get(start_node_id, [])
        if not start_edges:
            raise WorkflowValidationError('The start node has no outgoing edge to a step.')

        first_step_id = start_edges[0]['target']
        if first_step_id not in step_nodes:
            raise WorkflowValidationError('The start node must connect directly to a step node.')

        graph.add_edge(START, cls._agent_node_name(first_step_id))

        return graph.compile()

    @classmethod
    def _parse_and_validate(cls, workflow_json):
        workflow_json = workflow_json or {}
        raw_nodes = workflow_json.get('nodes') or []
        raw_edges = workflow_json.get('edges') or []

        if not raw_nodes:
            raise WorkflowValidationError('This workflow has no nodes.')

        nodes_by_id = {}
        for node in raw_nodes:
            node_id = node.get('id')
            node_type = node.get('type')
            if not node_id or node_type not in (START_NODE_TYPE, STEP_NODE_TYPE, END_NODE_TYPE):
                raise WorkflowValidationError(f'Node {node!r} is missing an id or has an invalid type.')
            nodes_by_id[node_id] = node

        cls._find_single(nodes_by_id, START_NODE_TYPE, 'start')

        for node_id, node in nodes_by_id.items():
            if node['type'] != STEP_NODE_TYPE:
                continue
            data = node.get('data') or {}
            if not data.get('name'):
                raise WorkflowValidationError(f'Step node {node_id!r} is missing its name/prompt slug.')
            for tool_id in data.get('tools') or []:
                if not ToolRegistry.is_valid(tool_id):
                    raise WorkflowValidationError(f'Step node {node_id!r} references an unknown tool {tool_id!r}.')

        for edge in raw_edges:
            if edge.get('source') not in nodes_by_id or edge.get('target') not in nodes_by_id:
                raise WorkflowValidationError(f'Edge {edge!r} references a node that does not exist.')

        return nodes_by_id, raw_edges

    @staticmethod
    def _find_single(nodes_by_id, node_type, label):
        matches = [node_id for node_id, node in nodes_by_id.items() if node['type'] == node_type]
        if not matches:
            raise WorkflowValidationError(f'This workflow has no {label} node.')
        return matches[0]

    @staticmethod
    def _agent_node_name(step_id):
        return f'{step_id}__agent'

    @staticmethod
    def _tools_node_name(step_id):
        return f'{step_id}__tools'

    @classmethod
    def _build_step_callables(cls, step, company_id, conversation):
        step_id = step['id']
        data = step.get('data') or {}
        prompt_slug = data['name']
        tool_ids = data.get('tools') or []

        system_prompt, config = PromptService.get_prompt_and_config(
            prompt_slug, fallback=_default_fallback_prompt(tool_ids),
        )
        tools = ToolRegistry.build_tools(tool_ids, company_id, conversation)
        has_tools = bool(tools)

        llm = get_chat_model(streaming=True, temperature=config.get('temperature', 0.7))
        llm_with_tools = llm.bind_tools(tools) if has_tools else llm

        async def call_agent(state):
            messages = state['messages']
            if not messages or messages[0].type != 'system':
                messages = [SystemMessage(content=system_prompt), *messages]
            response = await llm_with_tools.ainvoke(messages)
            return {'messages': [response]}

        call_tools = None
        if has_tools:
            tool_node = ToolNode(tools)

            async def call_tools(state):
                result = await tool_node.ainvoke(state)
                new_messages = result.get('messages', [])
                called_names = [message.name for message in new_messages if isinstance(message, ToolMessage)]

                step_tool_calls = dict(state.get('step_tool_calls') or {})
                step_tool_calls[step_id] = step_tool_calls.get(step_id, []) + called_names

                return {'messages': new_messages, 'step_tool_calls': step_tool_calls}

        return call_agent, call_tools, has_tools

    @classmethod
    def _build_step_router(cls, step_id, tools_node_name, outgoing_edges, nodes_by_id, has_tools):
        def resolve_target(node_id):
            if nodes_by_id[node_id]['type'] == END_NODE_TYPE:
                return END
            return cls._agent_node_name(node_id)

        def route(state):
            last_message = state['messages'][-1]

            if has_tools and getattr(last_message, 'tool_calls', None):
                return tools_node_name

            called_tools = set((state.get('step_tool_calls') or {}).get(step_id, []))

            for edge in outgoing_edges:
                condition = (edge.get('data') or {}).get('condition', CONDITION_ALWAYS)

                if condition == CONDITION_ALWAYS:
                    return resolve_target(edge['target'])
                if condition.startswith(CONDITION_TOOL_CALLED_PREFIX):
                    tool_name = condition[len(CONDITION_TOOL_CALLED_PREFIX):]
                    if tool_name in called_tools:
                        return resolve_target(edge['target'])
                elif condition.startswith(CONDITION_TOOL_NOT_CALLED_PREFIX):
                    tool_name = condition[len(CONDITION_TOOL_NOT_CALLED_PREFIX):]
                    if tool_name not in called_tools:
                        return resolve_target(edge['target'])

            # No outgoing edge matched (or none exist at all) -- this step doubles as
            # the workflow's end, per the "step can act as an end node" design.
            return END

        return route
