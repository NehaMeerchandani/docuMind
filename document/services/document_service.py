from document.models import Chunk, DocumentStatus
from document.services.chunking_service import ChunkingService
from document.services.embedding_service import EmbeddingService
from document.services.parser_service import ParserService


class DocumentService:
    @classmethod
    def process(cls, document):
        if document.status != DocumentStatus.PENDING:
            raise ValueError('This document has already been processed.')

        document.status = DocumentStatus.PROCESSING
        document.save(update_fields=['status'])

        try:
            doc_type, text = ParserService.fetch_and_parse(document.source_url)
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
            raise

        return document
