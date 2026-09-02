class ChunkingService:
    CHUNK_SIZE = 1000
    CHUNK_OVERLAP = 150

    @classmethod
    def split_text(cls, text):
        text = text.strip()
        if not text:
            return []

        chunks = []
        start = 0
        text_length = len(text)

        while start < text_length:
            end = start + cls.CHUNK_SIZE
            piece = text[start:end].strip()
            if piece:
                chunks.append(piece)
            if end >= text_length:
                break
            start = end - cls.CHUNK_OVERLAP

        return chunks
