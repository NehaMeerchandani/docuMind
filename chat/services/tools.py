from asgiref.sync import sync_to_async
from langchain_core.tools import tool

from chat.models import MessageType
from chat.services.llm_client import get_chat_model
from chat.services.vector_service import VectorService
from document.models import Chunk, Document, DocumentStatus


def make_search_documents_tool(company_id):
    @tool
    async def search_documents(query: str) -> str:
        """Search this company's uploaded documents for information relevant to the query.

        Use this whenever the question might be answered by the company's own documents
        (e.g. product details, internal processes, specific facts from uploaded content).
        Do not use this for general knowledge or small-talk questions unrelated to the
        company's documents.
        """
        results = await sync_to_async(VectorService.search)(query, company_id)
        chunk_ids = [result.id for result in results]

        chunks = await sync_to_async(list)(Chunk.objects.filter(id__in=chunk_ids))
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        ordered_chunks = [chunks_by_id[cid] for cid in chunk_ids if cid in chunks_by_id]

        if not ordered_chunks:
            return 'No relevant documents were found for this query.'

        return '\n\n'.join(f'[{i + 1}] {chunk.content}' for i, chunk in enumerate(ordered_chunks))

    return search_documents


def make_summarize_session_tool(conversation):
    @tool
    async def summarize_session() -> str:
        """Summarize the current conversation so far.

        Use this only when the user explicitly asks for a summary of the conversation
        or session (e.g. "summarize this chat", "what have we talked about").
        """
        messages = await sync_to_async(list)(
            conversation.messages.filter(message_type=MessageType.TEXT).order_by('created_at'),
        )

        if not messages:
            return 'There is nothing in this conversation yet to summarize.'

        transcript = '\n'.join(f'{message.role}: {message.message}' for message in messages)

        llm = get_chat_model()
        response = await llm.ainvoke([
            {'role': 'system', 'content': 'Summarize the following conversation concisely, in a few sentences.'},
            {'role': 'user', 'content': transcript},
        ])
        summary = response.content

        # Overwritten each time -- only the latest summary is kept, not a history of them.
        conversation.summary = summary
        await sync_to_async(conversation.save)(update_fields=['summary'])

        return summary

    return summarize_session


def make_list_document_process_tool(company_id):
    @tool
    async def list_document_process() -> str:
        """List this company's documents and their processing status (completed,
        processing, pending, or failed), with counts per status.

        Use this whenever the user asks what documents exist, which have finished
        processing, or which are still pending/queued/in progress.
        """
        documents = await sync_to_async(list)(
            Document.objects.filter(company_id=company_id).order_by('-id'),
        )

        if not documents:
            return 'This company has no uploaded documents yet.'

        counts = {choice_value: 0 for choice_value, _label in DocumentStatus.CHOICES}
        lines = []

        for document in documents:
            counts[document.status] = counts.get(document.status, 0) + 1
            label = document.title or document.source_url or f'Document {document.pk}'
            lines.append(f'- {label}: {document.get_status_display()}')

        counts_summary = ', '.join(
            f'{counts[value]} {label.lower()}'
            for value, label in DocumentStatus.CHOICES
            if counts[value]
        )

        return (
            f'Counts -- {counts_summary or "none"}.\n\n'
            'Documents:\n' + '\n'.join(lines)
        )

    return list_document_process
