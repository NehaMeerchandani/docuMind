import uuid

from django.conf import settings
from django.db import models

from base.models import CompanyBaseModel, SoftDeleteManager, SoftDeleteQuerySet


class MessageType:
    TEXT = 'text'
    ERROR = 'error'
    SYSTEM = 'system'
    CONTEXT = 'context'

    CHOICES = (
        (TEXT, 'Text'),
        (ERROR, 'Error'),
        (SYSTEM, 'System'),
        (CONTEXT, 'Context'),
    )


class MessageSender:
    USER = 'user'
    ASSISTANT = 'assistant'

    CHOICES = (
        (USER, 'User'),
        (ASSISTANT, 'Assistant'),
    )


class ConversationQuerySet(SoftDeleteQuerySet):
    def delete(self):
        Message.objects.filter(conversation__in=self).delete()
        return super().delete()


class ConversationManager(SoftDeleteManager):
    def get_queryset(self):
        return ConversationQuerySet(self.model, using=self._db).alive()


class Conversation(CompanyBaseModel):
    session_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversations',
    )
    title = models.CharField(max_length=255, blank=True)

    objects = ConversationManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title or str(self.session_id)

    def delete(self, using=None, keep_parents=False):
        self.messages.all().delete()
        super().delete(using=using, keep_parents=keep_parents)


class Message(CompanyBaseModel):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.CharField(max_length=20, choices=MessageSender.CHOICES)
    message_type = models.CharField(max_length=20, choices=MessageType.CHOICES, default=MessageType.TEXT)
    content = models.TextField(blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.sender}: {self.content[:50]}'
