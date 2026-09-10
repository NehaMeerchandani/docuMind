from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone

from document.models import Chunk, DocumentStatus, DocumentType
from document.services.chunking_service import ChunkingService
from document.services.embedding_service import EmbeddingService
from document.services.parser_service import ParserService


class DocumentService:
    @classmethod
    def process(cls, document, user=None):
        if document.status == DocumentStatus.COMPLETED:
            raise ValueError('This document has already been processed.')

        document.chunks.all().delete()

        document.status = DocumentStatus.PROCESSING
        if user is not None:
            document.updated_by = user
        document.save(update_fields=['status', 'updated_by'] if user is not None else ['status'])

        try:
            if document.source_url:
                doc_type, text = ParserService.fetch_and_parse(document.source_url)
            else:
                doc_type, text = DocumentType.TEXT, document.content

            chunks_text = ChunkingService.split_text(text)

            if not chunks_text:
                raise ValueError('No extractable text content was found at this URL.')

            for index, chunk_text in enumerate(chunks_text):
                chunk = Chunk.objects.create(
                    company=document.company,
                    document=document,
                    content=chunk_text,
                    chunk_index=index,
                    created_by=document.created_by,
                )
                EmbeddingService.upsert_chunk(chunk)

            document.doc_type = doc_type
            document.status = DocumentStatus.COMPLETED
            document.error_message = ''
            document.save(update_fields=['doc_type', 'status', 'error_message'])

        except Exception as exc:
            document.status = DocumentStatus.FAILED
            document.error_message = str(exc)
            document.save(update_fields=['status', 'error_message'])
            cls._notify_uploader(document)
            raise

        cls._notify_uploader(document)
        return document

    @classmethod
    def _notify_uploader(cls, document):
        """Tells `document.created_by` (whoever uploaded it) that this document has
        finished processing -- not "whichever conversation happened to ask about
        pending documents" (that per-conversation-watch design was replaced with this
        simpler per-user one).

        Runs from both the Celery task and the Kafka consumer (both call
        DocumentService.process directly), so it's the single place this fires
        regardless of which async path processed the document.

        The notification Message is always written to the uploader's most recent
        conversation first, unconditionally -- this is what makes it show up in chat
        history even if the uploader has no websocket connected right now (live push
        is best-effort; history is guaranteed). The channel-layer group_send to
        `user_<id>` (see ChatConsumer.connect, chat/consumers.py) is a separate,
        best-effort step: if Channels/Redis isn't reachable, or the uploader simply
        isn't connected right now, the already-saved Message is unaffected -- they'll
        see it next time they open that conversation.
        """
        if document.created_by_id is None:
            return

        from chat.models import Conversation, Message, MessageRole, MessageType

        conversation = (
            Conversation.objects.filter(user_id=document.created_by_id, company=document.company)
            .order_by('-created_at')
            .first()
        )
        if conversation is None:
            return

        label = document.title or document.source_url or f'Document {document.pk}'
        notification_text = f'"{label}" has finished processing: {document.get_status_display()}.'

        message = Message.objects.create(
            company=document.company,
            conversation=conversation,
            role=MessageRole.ASSISTANT,
            message_type=MessageType.SYSTEM,
            message=notification_text,
        )

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        try:
            async_to_sync(channel_layer.group_send)(
                f'user_{document.created_by_id}',
                {
                    'type': 'document.notification',
                    'message': {
                        'id': message.id,
                        'document_id': document.pk,
                        'status': document.status,
                        'message': notification_text,
                    },
                },
            )
        except Exception:
            pass
