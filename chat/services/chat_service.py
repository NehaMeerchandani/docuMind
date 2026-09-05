import json

from asgiref.sync import sync_to_async
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from chat.models import Message, MessageSender, MessageType
from chat.services.agent import build_agent, get_langfuse_handler
from chat.services.prompt_service import PromptService

TOOL_RESULT_PREVIEW_LENGTH = 200


class ChatService:

    @classmethod
    async def stream_reply(cls, conversation, company, question):
        history = await sync_to_async(PromptService.build_history)(conversation)

        await Message.objects.acreate(
            company=company,
            conversation=conversation,
            sender=MessageSender.USER,
            message_type=MessageType.TEXT,
            content=question,
            created_by_id=conversation.user_id,
        )

        if not conversation.title:
            conversation.title = question[:60]
            await conversation.asave(update_fields=['title'])

        agent = await sync_to_async(build_agent)(company.id, conversation)
        langfuse_handler = get_langfuse_handler()

        agent_input = {'messages': [*history, HumanMessage(content=question)]}
        run_config = {
            'callbacks': [langfuse_handler],
            'metadata': {
                'langfuse_session_id': str(conversation.session_id),
                'langfuse_user_id': str(conversation.user_id),
                'langfuse_tags': [f'company:{company.id}'],
            },
        }

        full_reply = ''
        started_tool_call_ids = set()

        try:
            async for stream_mode, payload in agent.astream(
                agent_input,
                config=run_config,
                stream_mode=['messages', 'updates'],
            ):
                if stream_mode == 'updates':
                    for event in cls._tool_end_events(payload):
                        yield event
                    continue

                message_chunk, metadata = payload

                if not isinstance(message_chunk, AIMessageChunk):
                    continue
                if metadata.get('langgraph_node') != 'agent':
                    continue

                for event in cls._tool_start_events(message_chunk, started_tool_call_ids):
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
                sender=MessageSender.ASSISTANT,
                message_type=MessageType.ERROR,
                content=error_message,
            )
            yield f'data: {json.dumps({"error": error_message})}\n\n'
            return

        await Message.objects.acreate(
            company=company,
            conversation=conversation,
            sender=MessageSender.ASSISTANT,
            message_type=MessageType.TEXT,
            content=full_reply,
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
    def _tool_end_events(cls, updates_payload):
        """Emit a `tool_end` event once a tool node finishes and its ToolMessage lands.

        `updates_payload` is the dict LangGraph's `stream_mode="updates"` yields for this
        step: `{node_name: {"messages": [...]}}`. We only care about the `tools` node's
        output here, since that's the only node whose messages are ToolMessage results.
        """
        node_output = updates_payload.get('tools')
        if not node_output:
            return

        for message in node_output.get('messages', []):
            if not isinstance(message, ToolMessage):
                continue

            content = message.content if isinstance(message.content, str) else str(message.content)
            preview = content[:TOOL_RESULT_PREVIEW_LENGTH]

            yield f'data: {json.dumps({"tool_end": {"name": message.name, "result_preview": preview}})}\n\n'
