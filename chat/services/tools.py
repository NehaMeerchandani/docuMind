import json

from asgiref.sync import sync_to_async
from langchain_core.tools import tool

from chat.models import Message, MessageRole, MessageType
from chat.services.chart_service import ChartBuildError, ChartService
from chat.services.llm_client import get_chat_model
from chat.services.table_parser_service import TableParseError, TableParserService
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


def make_build_chart_from_file_tool(conversation):
    @tool
    async def build_chart_from_file(chart_type: str = '', inline_data: str = '') -> str:
        """Build a chart/graph from tabular data. The data comes from one of two places:
        a CSV/Excel file the user attached to this conversation (now or earlier in this
        session), or data the user typed or pasted directly into their message.

        Call this whenever the user asks to create, build, make, plot, graph, chart, or
        visualize data. If they explicitly ask for a chart, create it -- never ask them
        to attach a file first when the data is already in their message.

        `inline_data`: only when the user typed or pasted the data into their message
        (e.g. a table or a list of values), pass it here converted to CSV text -- a
        header row, then one row per line, comma-separated, with numbers written plainly
        (12000, not 12,000) and no markdown or pipe characters. Example:
        "Month,Revenue,Expenses\\nJan,12000,8000\\nFeb,15000,9000". Leave it empty when
        charting an attached file.

        `chart_type` is optional -- pass "bar", "line", "pie", or "scatter" only if the
        user explicitly asked for that specific kind of chart. Leave it empty to let
        the chart be picked automatically based on the data's shape.
        """
        if inline_data.strip():
            try:
                table = await sync_to_async(TableParserService.parse_text)(inline_data)
            except TableParseError as exc:
                return f'Could not read the data from the message: {exc}'
            # Saved like an attached file so a follow-up ("make that a pie chart")
            # still finds the data without the user pasting it again.
            await sync_to_async(_save_inline_table)(conversation, table)
        else:
            table = await sync_to_async(_get_latest_attachment_table)(conversation)

        if table is None:
            return (
                'There is no data to chart: no file is attached in this conversation and '
                'no data was given in the message. Ask the user to attach a CSV or Excel '
                'file, or to include the data in their message.'
            )

        try:
            chart_payload = ChartService.build_chart(table, chart_type=chart_type or None)
        except ChartBuildError as exc:
            return f'Could not build a chart from this data: {exc}'

        # Returned as a JSON string like every other tool here (ToolMessage.content is
        # always text) -- ChatService.stream_reply is what recognizes the top-level
        # "chart" key and special-cases it into a `widget` SSE event + a MEDIA message,
        # instead of the usual 200-char-truncated tool_end preview every other tool
        # result gets.
        return json.dumps(chart_payload)

    return build_chart_from_file


def _save_inline_table(conversation, table):
    """Persists a table that came from the chat message text (not an uploaded file) in
    the same CONTEXT-row shape ChatService._persist_attachment_context uses for files,
    so _get_latest_attachment_table and PromptService.build_history treat both alike.
    """
    from chat.services.chart_service import MAX_ROWS_IN_CONTEXT

    payload = {
        'kind': 'attachment_table',
        'filename': 'data pasted in chat',
        'table': {'columns': table['columns'], 'rows': table['rows'][:MAX_ROWS_IN_CONTEXT]},
        'row_count': len(table['rows']),
    }
    Message.objects.create(
        company_id=conversation.company_id,
        conversation=conversation,
        role=MessageRole.ASSISTANT,
        message_type=MessageType.CONTEXT,
        message=json.dumps(payload),
    )


def _get_latest_attachment_table(conversation):
    """Fetches the most recently attached file's parsed table for this conversation,
    from the latest MessageType.CONTEXT row tagged kind="attachment_table" (see
    ChatService._persist_attachment_context, which writes this exact shape the moment
    a file is uploaded alongside a chat turn -- before the graph runs, so this same
    turn's own tool call can already see it). Returns None if no file has ever been
    attached in this conversation.

    A plain module-level function (not a method) since it's only ever called from
    inside the sync_to_async wrapper above -- no class/self needed. Filtering by
    message_type=CONTEXT alone (without the kind check) would be wrong if CONTEXT rows
    are ever used for anything else in the future.
    """
    context_messages = conversation.messages.filter(message_type=MessageType.CONTEXT).order_by('-created_at')

    for context_message in context_messages:
        if not context_message.message:
            continue
        try:
            payload = json.loads(context_message.message)
        except (TypeError, ValueError):
            continue

        if not isinstance(payload, dict) or payload.get('kind') != 'attachment_table':
            continue

        table = payload.get('table')
        if table and table.get('rows'):
            return table

    return None
