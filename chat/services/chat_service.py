import json

from asgiref.sync import sync_to_async
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from agents.graph.builder import WorkflowGraphBuilder
from agents.services.router_service import RouterService
from chat.models import Message, MessageRole, MessageType
from chat.services.agent import get_langfuse_handler
from chat.services.prompt_service import PromptService

TOOL_RESULT_PREVIEW_LENGTH = 200
NO_TOOL_MARKER = 'no_tool'
NO_AGENT_AVAILABLE_MESSAGE = (
    'No agent is configured yet. Create at least one in the Agents section of the admin.'
)

# If the user's question contains this phrase (case-insensitive), the agent is given
# only the conversation's stored summary as context, instead of recent message history.
# The two are never combined -- see ChatService.stream_reply.
USE_SUMMARY_PHRASE = 'use summary'


class ChatService:

    @classmethod
    async def stream_reply(cls, conversation, company, question):
        if USE_SUMMARY_PHRASE in question.lower():
            history = await sync_to_async(PromptService.build_summary_context)(conversation)
        else:
            history = await sync_to_async(PromptService.build_history)(conversation)

        await Message.objects.acreate(
            company=company,
            conversation=conversation,
            role=MessageRole.USER,
            message_type=MessageType.TEXT,
            message=question,
            created_by_id=conversation.user_id,
        )

        if not conversation.title:
            conversation.title = question[:60]
            await conversation.asave(update_fields=['title'])

        # The router picks which Agent handles THIS message specifically (re-decided on
        # every message, not once per conversation, per the user's explicit choice) --
        # a single conversation can be answered by a different agent turn to turn.
        chosen_agent = await RouterService.choose_agent(question)
        if chosen_agent is None:
            await Message.objects.acreate(
                company=company,
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                message_type=MessageType.ERROR,
                message=NO_AGENT_AVAILABLE_MESSAGE,
            )
            yield f'data: {json.dumps({"error": NO_AGENT_AVAILABLE_MESSAGE})}\n\n'
            return

        graph = await sync_to_async(WorkflowGraphBuilder.build)(chosen_agent, company.id, conversation)
        langfuse_handler = get_langfuse_handler()

        agent_input = {'messages': [*history, HumanMessage(content=question)], 'step_tool_calls': {}}
        run_config = {
            'callbacks': [langfuse_handler],
            'metadata': {
                'langfuse_session_id': str(conversation.session_id),
                'langfuse_user_id': str(conversation.user_id),
                'langfuse_tags': [f'company:{company.id}', f'agent:{chosen_agent.name}'],
            },
        }

        full_reply = ''
        started_tool_call_ids = set()
        any_tool_used = False

        try:
            async for stream_mode, payload in graph.astream(
                agent_input,
                config=run_config,
                stream_mode=['messages', 'updates'],
            ):
                if stream_mode == 'updates':
                    for tool_name, result_preview in cls._tool_end_results(payload):
                        await Message.objects.acreate(
                            company=company,
                            conversation=conversation,
                            role=MessageRole.ASSISTANT,
                            message_type=MessageType.TOOL,
                            tool_name=tool_name,
                            message=result_preview,
                        )
                        yield f'data: {json.dumps({"tool_end": {"name": tool_name, "result_preview": result_preview}})}\n\n'
                    continue

                message_chunk, metadata = payload

                if not isinstance(message_chunk, AIMessageChunk):
                    continue
                # Every step's LLM-calling node is named "<node_id>__agent" (see
                # WorkflowGraphBuilder._agent_node_name) -- unlike the old single fixed
                # agent, there's no one node name to check for exact equality anymore.
                if not (metadata.get('langgraph_node') or '').endswith('__agent'):
                    continue

                for event in cls._tool_start_events(message_chunk, started_tool_call_ids):
                    any_tool_used = True
                    yield event

                delta = message_chunk.content
                if delta:
                    full_reply += delta
                    yield f'data: {json.dumps({"content": delta})}\n\n'

        except Exception as exc:
            error_message = f'{type(exc).__name__}: {exc}' if str(exc) else type(exc).__name__
            await Message.objects.acreate(
                company=company,
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                message_type=MessageType.ERROR,
                message=error_message,
            )
            yield f'data: {json.dumps({"error": error_message})}\n\n'
            return

        if not any_tool_used:
            await Message.objects.acreate(
                company=company,
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                message_type=MessageType.TOOL,
                tool_name=NO_TOOL_MARKER,
                message='Answered directly by the model, no tool used.',
            )

        await Message.objects.acreate(
            company=company,
            conversation=conversation,
            role=MessageRole.ASSISTANT,
            message_type=MessageType.TEXT,
            message=full_reply,
        )

        yield 'data: [DONE]\n\n'

    @classmethod
    def _tool_start_events(cls, message_chunk, started_tool_call_ids):
        """Emit a `tool_start` event the moment the model picks a tool to call.

        `tool_call_chunks` on an AIMessageChunk carry the tool's name on the very first
        fragment of a given tool call (before its arguments have even finished streaming),
        so this is the earliest point we can tell the client "a tool is about to run."
        `started_tool_call_ids` dedupes this, since the same tool call's id repeats across
        every subsequent argument-streaming chunk.
        """
        for tool_call_chunk in message_chunk.tool_call_chunks:
            call_id = tool_call_chunk.get('id')
            name = tool_call_chunk.get('name')

            if not call_id or not name or call_id in started_tool_call_ids:
                continue

            started_tool_call_ids.add(call_id)
            yield f'data: {json.dumps({"tool_start": {"name": name}})}\n\n'

    @classmethod
    def _tool_end_results(cls, updates_payload):
        """Yield (tool_name, result_preview) once any step's tools node finishes and its
        ToolMessage lands.

        `updates_payload` is the dict LangGraph's `stream_mode="updates"` yields for this
        step: `{node_name: {"messages": [...]}}`. Every step's tool-executing node is
        named "<node_id>__tools" (see WorkflowGraphBuilder._tools_node_name) -- we don't
        care which specific step it came from here, just that it's a tools node.
        """
        for node_name, node_output in updates_payload.items():
            if not node_name.endswith('__tools') or not node_output:
                continue

            for message in node_output.get('messages', []):
                if not isinstance(message, ToolMessage):
                    continue

                content = message.content if isinstance(message.content, str) else str(message.content)
                preview = content[:TOOL_RESULT_PREVIEW_LENGTH]

                yield message.name, preview
