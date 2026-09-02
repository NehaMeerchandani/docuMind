import json
import os

import httpx
from asgiref.sync import sync_to_async

from chat.models import Message, MessageSender, MessageType
from chat.services.prompt_service import PromptService
from chat.services.vector_service import VectorService
from document.models import Chunk


class ChatService:
    LLM_BASE_URL = os.getenv('LLM_BASE_URL')
    LLM_API_KEY = os.getenv('LLM_API_KEY')
    LLM_MODEL = os.getenv('LLM_MODEL')

    @classmethod
    async def stream_reply(cls, conversation, company, question):
        ordered_chunks = await cls._search_chunks(question, company.id)
        messages = await sync_to_async(PromptService.build_messages)(conversation, ordered_chunks, question)

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

        full_reply = ''

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream(
                    'POST',
                    f'{cls.LLM_BASE_URL}/chat/completions',
                    headers={'Authorization': f'Bearer {cls.LLM_API_KEY}'},
                    json={'model': cls.LLM_MODEL, 'messages': messages, 'stream': True},
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not line or not line.startswith('data: '):
                            continue

                        payload = line[len('data: '):]
                        if payload.strip() == '[DONE]':
                            break

                        delta = json.loads(payload)['choices'][0]['delta'].get('content', '')
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
    async def _search_chunks(cls, question, company_id):
        results = await sync_to_async(VectorService.search)(question, company_id)
        chunk_ids = [result.id for result in results]

        chunks = await sync_to_async(list)(Chunk.objects.filter(id__in=chunk_ids))
        chunks_by_id = {chunk.id: chunk for chunk in chunks}

        return [chunks_by_id[cid] for cid in chunk_ids if cid in chunks_by_id]
