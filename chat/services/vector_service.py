from qdrant_client.http import models as qdrant_models

from document.services.embedding_service import EmbeddingService


class VectorService:
    TOP_K = 5

    @classmethod
    def search(cls, query_text, company_id):
        embedding = EmbeddingService.generate_embedding(query_text)
        client = EmbeddingService.get_client()

        return client.search(
            collection_name=EmbeddingService.COLLECTION_NAME,
            query_vector=embedding,
            query_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key='company_id',
                        match=qdrant_models.MatchValue(value=company_id),
                    ),
                ],
            ),
            limit=cls.TOP_K,
        )
