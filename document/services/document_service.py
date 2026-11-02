from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone

from document.models import Chunk, DocumentStatus, DocumentType
from document.services.chunking_service import ChunkingService
from document.services.embedding_service import EmbeddingService
from document.services.parser_service import ParserService


class ProcessingStep:
    """Values of Document.processing_step: where inside status=PROCESSING a document is."""
    PARSING = 'parsing'
    CHUNKING = 'chunking'
    EMBEDDING = 'embedding'
    DONE = 'done'


class DocumentService:
    """Document processing, split into small steps.

    `process()` runs them one after another in-process (what the admin button, the REST
    API, Celery and Kafka call). The Temporal activities (document/temporal/activities.py)
    call the very same step functions, one per activity, so there is one implementation
    of each step and only the orchestration differs.
    """

    @classmethod
    def process(cls, document, user=None):
        if document.status == DocumentStatus.COMPLETED:
            raise ValueError('This document has already been processed.')

        cls.mark_processing(document, user=user)

        try:
            doc_type, text = cls.parse(document)
            chunk_ids = cls.create_chunks(document, text)
            cls.embed_chunks(chunk_ids)
        except Exception as exc:
            cls.mark_failed(document, str(exc))
            raise

        return cls.mark_completed(document, doc_type)

    @classmethod
    def mark_processing(cls, document, user=None):
        document.status = DocumentStatus.PROCESSING
        document.processing_step = ProcessingStep.PARSING
        update_fields = ['status', 'processing_step']
        if user is not None:
            document.updated_by = user
            update_fields.append('updated_by')
        document.save(update_fields=update_fields)

    @classmethod
    def set_step(cls, document, step):
        document.processing_step = step
        document.save(update_fields=['processing_step'])

    @classmethod
    def parse(cls, document):
        """Returns (doc_type, text) from the document's uploaded file, URL or pasted text."""
        cls.set_step(document, ProcessingStep.PARSING)

        if document.file:
            with document.file.open('rb') as stored_file:
                content = stored_file.read()
            return ParserService.parse_file(document.file.name, content)
        if document.source_url:
            return ParserService.fetch_and_parse(document.source_url)
        return DocumentType.TEXT, document.content

    @classmethod
    def create_chunks(cls, document, text):
        """Splits `text` into Chunk rows and returns their ids (not yet embedded).

        Deletes the document's existing chunks first, so re-running this step (a retry,
        or a re-process) never leaves duplicates behind.
        """
        cls.set_step(document, ProcessingStep.CHUNKING)
        document.chunks.all().delete()

        chunks_text = ChunkingService.split_text(text)
        if not chunks_text:
            raise ValueError('No extractable text content was found at this URL.')

        chunks = [
            Chunk.objects.create(
                company=document.company,
                document=document,
                content=chunk_text,
                chunk_index=index,
                created_by=document.created_by,
            )
            for index, chunk_text in enumerate(chunks_text)
        ]
        return [chunk.id for chunk in chunks]

    @classmethod
    def embed_chunks(cls, chunk_ids, on_chunk_embedded=None):
        """Embeds the given chunks into Qdrant, in order. Safe to repeat: a chunk's
        point id is its own id, so embedding it again overwrites rather than duplicates.

        `on_chunk_embedded(done, total)`, if given, is called after each chunk (the
        Temporal activity uses it to heartbeat).
        """
        chunks = list(Chunk.objects.filter(id__in=chunk_ids).order_by('chunk_index'))
        if chunks:
            cls.set_step(chunks[0].document, ProcessingStep.EMBEDDING)

        for done, chunk in enumerate(chunks, start=1):
            EmbeddingService.upsert_chunk(chunk)
            if on_chunk_embedded is not None:
                on_chunk_embedded(done, len(chunks))

    @classmethod
    def mark_completed(cls, document, doc_type):
        document.doc_type = doc_type
        document.status = DocumentStatus.COMPLETED
        document.processing_step = ProcessingStep.DONE
        document.error_message = ''
        document.save(update_fields=['doc_type', 'status', 'processing_step', 'error_message'])
        cls._notify_uploader(document)
        return document

    @classmethod
    def mark_failed(cls, document, error_message):
        document.status = DocumentStatus.FAILED
        document.error_message = error_message
        document.save(update_fields=['status', 'error_message'])
        cls._notify_uploader(document)

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
