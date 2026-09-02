from rest_framework import serializers

from document.models import Chunk, Document, DocumentType


class DocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = [
            'id', 'company', 'source_url', 'content', 'title', 'doc_type',
            'status', 'error_message', 'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'company', 'doc_type', 'status', 'error_message',
            'created_by', 'created_at', 'updated_at',
        ]


class DocumentUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ['source_url', 'content', 'title']

    def validate(self, attrs):
        source_url = attrs.get('source_url', '')
        content = attrs.get('content', '')

        if bool(source_url) == bool(content):
            raise serializers.ValidationError('Provide exactly one of source_url or content, not both.')

        if content:
            attrs['doc_type'] = DocumentType.TEXT

        return attrs


class ChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chunk
        fields = ['id', 'chunk_index', 'content']
