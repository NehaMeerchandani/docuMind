import os

import requests
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models


class EmbeddingService:
    OLLAMA_BASE_URL = os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')
    OLLAMA_MODEL = os.getenv('OLLAMA_EMBEDDING_MODEL', 'nomic-embed-text')
    EMBEDDING_DIMENSIONS = int(os.getenv('EMBEDDING_DIMENSIONS', '768'))

    QDRANT_HOST = os.getenv('QDRANT_HOST', 'localhost')
    QDRANT_PORT = int(os.getenv('QDRANT_PORT', '6333'))
    COLLECTION_NAME = os.getenv('QDRANT_COLLECTION_NAME', 'documind_chunks')

    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is None:
            cls._client = QdrantClient(host=cls.QDRANT_HOST, port=cls.QDRANT_PORT)
        return cls._client

    @classmethod
    def ensure_collection(cls):
        client = cls.get_client()
        if not client.collection_exists(cls.COLLECTION_NAME):
            client.create_collection(
                collection_name=cls.COLLECTION_NAME,
                vectors_config=qdrant_models.VectorParams(
                    size=cls.EMBEDDING_DIMENSIONS,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )

    @classmethod
    def generate_embedding(cls, text):
        response = requests.post(
            f'{cls.OLLAMA_BASE_URL}/api/embeddings',
            json={'model': cls.OLLAMA_MODEL, 'prompt': text},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()['embedding']

    @classmethod
    def delete_points(cls, point_ids):
        if not point_ids:
            return

        client = cls.get_client()
        client.delete(
            collection_name=cls.COLLECTION_NAME,
            points_selector=qdrant_models.PointIdsList(points=point_ids),
        )

    @classmethod
    def upsert_chunk(cls, chunk):
        cls.ensure_collection()
        embedding = cls.generate_embedding(chunk.content)
        client = cls.get_client()
        client.upsert(
            collection_name=cls.COLLECTION_NAME,
            points=[
                qdrant_models.PointStruct(
                    id=chunk.id,
                    vector=embedding,
                    payload={
                        'company_id': chunk.company_id,
                        'document_id': chunk.document_id,
                        'content': chunk.content,
                    },
                ),
            ],
        )
