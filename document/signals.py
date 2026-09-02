from django.db.models.signals import post_delete
from django.dispatch import receiver

from document.models import Chunk
from document.services.embedding_service import EmbeddingService


@receiver(post_delete, sender=Chunk)
def cleanup_qdrant_point(sender, instance, **kwargs):
    EmbeddingService.delete_points([instance.id])
