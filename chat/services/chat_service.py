import json

from asgiref.sync import sync_to_async
from langchain_core.messages import AIMessageChunk, HumanMessage

from chat.models import Message, MessageSender, MessageType
from chat.services.agent import build_agent, get_langfuse_handler
from chat.services.prompt_service import PromptService


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

        try:
            async for stream_mode, payload in agent.astream(
                agent_input,
                config=run_config,
                stream_mode=['messages'],
            ):
                message_chunk, metadata = payload

                if not isinstance(message_chunk, AIMessageChunk):
                    continue
                if metadata.get('langgraph_node') != 'agent':
                    continue

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
