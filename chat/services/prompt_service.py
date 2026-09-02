from chat.models import MessageSender, MessageType


class PromptService:
    SYSTEM_PROMPT = (
        'You are a helpful assistant that answers questions using only the provided context. '
        "If the context doesn't contain the answer, say you don't know."
    )

    @classmethod
    def build_messages(cls, conversation, chunks, question):
        messages = [{'role': 'system', 'content': cls.SYSTEM_PROMPT}]

        history = conversation.messages.filter(message_type=MessageType.TEXT).order_by('created_at')
        for msg in history:
            role = 'user' if msg.sender == MessageSender.USER else 'assistant'
            messages.append({'role': role, 'content': msg.content})

        if chunks:
            context_text = '\n\n'.join(f'[{i + 1}] {chunk.content}' for i, chunk in enumerate(chunks))
            user_content = f'Context:\n{context_text}\n\nQuestion: {question}'
        else:
            user_content = question

        messages.append({'role': 'user', 'content': user_content})

        return messages
