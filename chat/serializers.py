from rest_framework import serializers

from chat.models import Conversation, Message


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ['id', 'role', 'message_type', 'message', 'tool_name', 'created_at']


class ConversationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Conversation
        fields = ['session_id', 'title', 'summary', 'created_at', 'updated_at']
        read_only_fields = ['session_id', 'title', 'summary', 'created_at', 'updated_at']
