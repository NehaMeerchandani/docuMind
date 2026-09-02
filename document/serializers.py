from rest_framework import serializers

from document.models import Chunk, Document


class DocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = [
            'id', 'company', 'source_url', 'title', 'doc_type',
            'status', 'error_message', 'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'company', 'doc_type', 'status', 'error_message',
            'created_by', 'created_at', 'updated_at',
        ]


class DocumentUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ['source_url', 'title']


class ChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chunk
        fields = ['id', 'chunk_index', 'content']
