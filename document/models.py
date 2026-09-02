from django.db import models

from base.models import CompanyBaseModel, SoftDeleteManager, SoftDeleteQuerySet
from basic.validators.url_validator import SSRFSafeURLValidator
from document.services.embedding_service import EmbeddingService


class DocumentType:
    WEBPAGE = 'webpage'
    PDF = 'pdf'
    TEXT = 'text'

    CHOICES = (
        (WEBPAGE, 'Webpage'),
        (PDF, 'PDF'),
        (TEXT, 'Text'),
    )


class DocumentStatus:
    PENDING = 'pending'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'

    CHOICES = (
        (PENDING, 'Pending'),
        (PROCESSING, 'Processing'),
        (COMPLETED, 'Completed'),
        (FAILED, 'Failed'),
    )


class DocumentQuerySet(SoftDeleteQuerySet):
    def delete(self):
        Chunk.objects.filter(document__in=self).delete()
        return super().delete()


class DocumentManager(SoftDeleteManager):
    def get_queryset(self):
        return DocumentQuerySet(self.model, using=self._db).alive()


class Document(CompanyBaseModel):
    source_url = models.URLField(max_length=2048, validators=[SSRFSafeURLValidator()])
    title = models.CharField(max_length=255, blank=True)
    doc_type = models.CharField(max_length=20, choices=DocumentType.CHOICES, blank=True)
    status = models.CharField(max_length=20, choices=DocumentStatus.CHOICES, default=DocumentStatus.PENDING)
    error_message = models.TextField(blank=True)

    objects = DocumentManager()

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return self.title or self.source_url

    def delete(self, using=None, keep_parents=False):
        self.chunks.all().delete()
        super().delete(using=using, keep_parents=keep_parents)


class ChunkQuerySet(SoftDeleteQuerySet):
    def delete(self):
        point_ids = list(self.values_list('id', flat=True))
        EmbeddingService.delete_points(point_ids)
        return super().delete()


class ChunkManager(SoftDeleteManager):
    def get_queryset(self):
        return ChunkQuerySet(self.model, using=self._db).alive()


class Chunk(CompanyBaseModel):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='chunks')
    content = models.TextField()
    chunk_index = models.PositiveIntegerField()

    objects = ChunkManager()

    class Meta:
        ordering = ['document_id', 'chunk_index']

    def __str__(self):
        return f'document {self.document_id} chunk #{self.chunk_index}'

    def delete(self, using=None, keep_parents=False):
        EmbeddingService.delete_points([self.id])
        super().delete(using=using, keep_parents=keep_parents)
