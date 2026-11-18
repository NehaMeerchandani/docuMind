import json
import math
import os

from asgiref.sync import sync_to_async
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from agents.graph.builder import WorkflowGraphBuilder
from agents.models import Agent
from agents.services.router_service import RouterService
from chat.models import Message, MessageRole, MessageType
from chat.services.agent import get_langfuse_handler
from chat.services.prompt_service import PromptService

TOOL_RESULT_PREVIEW_LENGTH = 200
NO_TOOL_MARKER = 'no_tool'
NO_AGENT_AVAILABLE_MESSAGE = (
    'No agent is configured yet. Create at least one in the Agents section of the admin.'
)

# Marks a persisted CONTEXT Message's payload as chart data (an attached file's table,
# or data pasted into the chat), as opposed to any other future use of CONTEXT rows.
# _persist_attachment_context below writes this exact shape, and
# chat.services.tools._get_latest_attachment_table and PromptService.build_history
# read it back:
# {"kind": "attachment_table", "filename": ..., "table": {"columns": [...], "rows": [...]}}.
# (tools._save_inline_table writes the same shape for pasted data, using the literal
# string below rather than importing this constant.)
ATTACHMENT_CONTEXT_KIND = 'attachment_table'
# Any uploaded file: only its stored path is remembered, not its text or rows.
ATTACHMENT_FILE_CONTEXT_KIND = 'attachment_file'
TABULAR_EXTENSIONS = {'.csv', '.xlsx', '.xls'}

# If the user's question contains this phrase (case-insensitive), the agent is given
# only the conversation's stored summary as context, instead of recent message history.
# The two are never combined -- see ChatService.stream_reply.
USE_SUMMARY_PHRASE = 'use summary'


class ChatService:

    @classmethod
    async def stream_reply(cls, conversation, company, question, attachment=None, agent=None):
        """`attachment`, if given, is {"filename": str, "stored_path": str} for a file just
        uploaded alongside this turn's question (see AdminChatStreamView.post, which saves
        it but does not parse it). Its path is persisted as a CONTEXT message BEFORE the
        graph runs (not after), so the tools in chat/services/tools.py can find it within
        this very same turn -- a file attached and asked about in one message must not
        require a second round-trip to "see" it.

        `agent`, if given, is the Agent that answers this turn and the router is skipped.
        Otherwise the conversation's own saved agent is used, and only a conversation
        with neither (a request to the agent-less /chat/stream/ endpoint) falls back to
        the router.
        """
        if USE_SUMMARY_PHRASE in question.lower():
            history = await sync_to_async(PromptService.build_summary_context)(conversation)
        else:
            history = await sync_to_async(PromptService.build_history)(conversation)

        await Message.objects.acreate(
            company=company,
            conversation=conversation,
            role=MessageRole.USER,
            message_type=MessageType.TEXT,
            message=question,
            created_by_id=conversation.user_id,
        )

        turn_hint = None
        if attachment:
            turn_hint = await sync_to_async(cls._persist_attachment_context)(company, conversation, attachment)

        if not conversation.title:
            conversation.title = question[:60]
            await conversation.asave(update_fields=['title'])

        # The router picks which Agent handles THIS message specifically (re-decided on
        # every message, not once per conversation, per the user's explicit choice) --
        # a single conversation can be answered by a different agent turn to turn.
        chosen_agent = agent
        if chosen_agent is None and conversation.agent_id:
            chosen_agent = await Agent.objects.filter(id=conversation.agent_id).afirst()
        if chosen_agent is not None and conversation.agent_id != chosen_agent.id:
            conversation.agent = chosen_agent
            await conversation.asave(update_fields=['agent'])
        if chosen_agent is None:
            chosen_agent = await RouterService.choose_agent(question)
        if chosen_agent is None:
            await Message.objects.acreate(
                company=company,
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                message_type=MessageType.ERROR,
                message=NO_AGENT_AVAILABLE_MESSAGE,
            )
            yield f'data: {json.dumps({"error": NO_AGENT_AVAILABLE_MESSAGE})}\n\n'
            return

        graph = await sync_to_async(WorkflowGraphBuilder.build)(chosen_agent, company.id, conversation)
        langfuse_handler = get_langfuse_handler()

        turn_messages = list(history)
        if turn_hint:
            # The hint goes after the history rather than before it, because the
            # per-step agent node (agents/graph/builder.py, call_agent) only adds the
            # step's own Langfuse system prompt when messages[0] is not already a system
            # message.
            #
            # KNOWN LIMITATION: that only holds when there is history. On the very first
            # message of a new conversation, history is empty, so this hint becomes
            # messages[0] and the Langfuse prompt is skipped for that one turn. Charting
            # still works (the hint itself tells the model to call the chart tool), and
            # every later turn is unaffected.
            turn_messages.append(SystemMessage(content=turn_hint))
        turn_messages.append(HumanMessage(content=question))

        agent_input = {'messages': turn_messages, 'step_tool_calls': {}}
        run_config = {
            'callbacks': [langfuse_handler],
            'metadata': {
                'langfuse_session_id': str(conversation.session_id),
                'langfuse_user_id': str(conversation.user_id),
                'langfuse_tags': [f'company:{company.id}', f'agent:{chosen_agent.name}'],
            },
        }

        full_reply = ''
        started_tool_call_ids = set()
        any_tool_used = False

        try:
            async for stream_mode, payload in graph.astream(
                agent_input,
                config=run_config,
                stream_mode=['messages', 'updates'],
            ):
                if stream_mode == 'updates':
                    for tool_name, raw_content in cls._tool_end_results(payload):
                        widget = cls._as_widget(raw_content)

                        if widget is not None:
                            widget_type, widget_data, widget_payload = widget
                            # Full structured data, not the usual 200-char text preview --
                            # a chart or table only renders correctly client-side with all
                            # of its data intact, not a truncated string.
                            await Message.objects.acreate(
                                company=company,
                                conversation=conversation,
                                role=MessageRole.ASSISTANT,
                                message_type=MessageType.MEDIA,
                                tool_name=tool_name,
                                message=json.dumps(widget_payload),
                            )
                            yield f'data: {json.dumps({"widget": {"type": widget_type, "data": widget_data}})}\n\n'
                            any_tool_used = True
                            continue

                        result_preview = raw_content[:TOOL_RESULT_PREVIEW_LENGTH]
                        await Message.objects.acreate(
                            company=company,
                            conversation=conversation,
                            role=MessageRole.ASSISTANT,
                            message_type=MessageType.TOOL,
                            tool_name=tool_name,
                            message=result_preview,
                        )
                        yield f'data: {json.dumps({"tool_end": {"name": tool_name, "result_preview": result_preview}})}\n\n'
                    continue

                message_chunk, metadata = payload

                if not isinstance(message_chunk, AIMessageChunk):
                    continue
                # Every step's LLM-calling node is named "<prompt_slug>__agent" (see
                # WorkflowGraphBuilder._assign_node_names) -- unlike the old single fixed
                # agent, there's no one node name to check for exact equality anymore.
                if not (metadata.get('langgraph_node') or '').endswith('__agent'):
                    continue

                for event in cls._tool_start_events(message_chunk, started_tool_call_ids):
                    any_tool_used = True
                    yield event

                delta = message_chunk.content
                if delta:
                    full_reply += delta
                    yield f'data: {json.dumps({"content": delta})}\n\n'

        except Exception as exc:
            error_message = f'{type(exc).__name__}: {exc}' if str(exc) else type(exc).__name__
            await Message.objects.acreate(
                company=company,
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                message_type=MessageType.ERROR,
                message=error_message,
            )
            yield f'data: {json.dumps({"error": error_message})}\n\n'
            return

        if attachment and not any_tool_used:
            # The assistant just asked what to do with the file: show the options as
            # buttons. Not saved -- they only make sense for this turn; a click sends the
            # button's text as a normal message.
            buttons = cls._upload_action_buttons(attachment['filename'])
            yield f'data: {json.dumps({"widget": {"type": "actions", "data": {"buttons": buttons}}})}\n\n'

        if not any_tool_used:
            await Message.objects.acreate(
                company=company,
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                message_type=MessageType.TOOL,
                tool_name=NO_TOOL_MARKER,
                message='Answered directly by the model, no tool used.',
            )

        await Message.objects.acreate(
            company=company,
            conversation=conversation,
            role=MessageRole.ASSISTANT,
            message_type=MessageType.TEXT,
            message=full_reply,
        )

        yield 'data: [DONE]\n\n'

    @classmethod
    def _persist_attachment_context(cls, company, conversation, attachment):
        """Persists a just-uploaded file's stored path as a CONTEXT Message, so it durably
        survives in this conversation's history (not a cache) -- reopening this chat
        tomorrow and saying "summarize that file" or "chart that" must still work without
        re-uploading. Only the path is kept, never the file's text or parsed rows: the
        tools (summarize_document, build_chart_from_file, process_document) read the
        stored file themselves, and only when the user actually asks for them.

        Written BEFORE the graph runs (not after), so a tool called within this very same
        turn can already find it.

        Returns a short string to use as this turn's own system hint, kept separate from
        what gets persisted since the persisted CONTEXT row is replayed on every later
        turn (see PromptService._build_attachment_reminder), while this hint is relevant
        only to the turn the file was just attached on.
        """
        filename = attachment['filename']
        stored_path = attachment['stored_path']

        Message.objects.create(
            company=company,
            conversation=conversation,
            role=MessageRole.ASSISTANT,
            message_type=MessageType.CONTEXT,
            message=json.dumps({
                'kind': ATTACHMENT_FILE_CONTEXT_KIND,
                'filename': filename,
                'stored_path': stored_path,
            }),
            # Lets the admin list every chat upload and open the file (chat/admin.py).
            media=stored_path,
            created_by_id=conversation.user_id,
        )

        is_tabular = os.path.splitext(filename)[1].lower() in TABULAR_EXTENSIONS
        attached = f'The user just attached a file "{filename}" (stored_path="{stored_path}").'
        if is_tabular:
            return (
                f'{attached} If their message does not already say what they want, ask: '
                '"What would you like me to do with this file? I can create a chart from it." '
                'CSV/Excel files can only be charted (build_chart_from_file). Do not guess.'
            )
        return (
            f'{attached} If their message does not already say what they want, ask exactly this, '
            'and nothing more: "What would you like me to do with this file? '
            '1. Process it (I will summarise it and keep the summary in this chat) '
            '2. Save it to the knowledge base". '
            'Meaning: "process it" -> summarize_with_temporal; "save it to the knowledge base" / '
            '"save this document" / "embed this" -> save_to_knowledge_base (it also summarises). '
            'Buttons for these two options are shown to the user automatically. Do not guess, and do not offer other options.'
        )

    @classmethod
    def _tool_start_events(cls, message_chunk, started_tool_call_ids):
        """Emit a `tool_start` event the moment the model picks a tool to call.

        `tool_call_chunks` on an AIMessageChunk carry the tool's name on the very first
        fragment of a given tool call (before its arguments have even finished streaming),
        so this is the earliest point we can tell the client "a tool is about to run."
        `started_tool_call_ids` dedupes this, since the same tool call's id repeats across
        every subsequent argument-streaming chunk.
        """
        for tool_call_chunk in message_chunk.tool_call_chunks:
            call_id = tool_call_chunk.get('id')
            name = tool_call_chunk.get('name')

            if not call_id or not name or call_id in started_tool_call_ids:
                continue

            started_tool_call_ids.add(call_id)
            yield f'data: {json.dumps({"tool_start": {"name": name}})}\n\n'

    @classmethod
    def _tool_end_results(cls, updates_payload):
        """Yield (tool_name, raw_content) once any step's tools node finishes and its
        ToolMessage lands. `raw_content` is the tool's full, untruncated string result --
        truncation to TOOL_RESULT_PREVIEW_LENGTH happens in the caller (stream_reply),
        conditionally, since a chart payload must not be truncated at all while every
        other tool's result still should be.

        `updates_payload` is the dict LangGraph's `stream_mode="updates"` yields for this
        step: `{node_name: {"messages": [...]}}`. Every step's tool-executing node is
        named "<prompt_slug>__tools" (see WorkflowGraphBuilder._assign_node_names) -- we don't
        care which specific step it came from here, just that it's a tools node.
        """
        for node_name, node_output in updates_payload.items():
            if not node_name.endswith('__tools') or not node_output:
                continue

            for message in node_output.get('messages', []):
                if not isinstance(message, ToolMessage):
                    continue

                content = message.content if isinstance(message.content, str) else str(message.content)

                yield message.name, content

    @classmethod
    def _upload_action_buttons(cls, filename):
        """The options offered right after a file is attached. Each `text` is sent as the
        user's next message."""
        if os.path.splitext(filename)[1].lower() in TABULAR_EXTENSIONS:
            return [{'label': 'Create a chart', 'text': 'Create a chart from this file', 'style': 'primary'}]
        return [
            {'label': 'Process it', 'text': 'Process this document', 'style': 'primary'},
            {'label': 'Save to knowledge base', 'text': 'Save this document to the knowledge base', 'style': 'success'},
        ]

    @classmethod
    def _as_widget(cls, raw_content):
        """(widget_type, widget_data, payload_to_persist) if `raw_content` is the JSON of a
        tool that renders as a widget, else None for every other tool's plain text:
        build_chart_from_file ("chart" key) or list_document_tasks ("table" key). A plain
        `json.loads` that happens to succeed on unrelated text is not enough.
        """
        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        if 'chart' in parsed:
            parsed = cls._json_safe(parsed)
            return 'chart', parsed['chart'], parsed
        if 'table' in parsed:
            # The note is for the model only; it is not part of what the UI stores.
            parsed = cls._json_safe(parsed)
            return 'task_table', parsed['table'], {'table': parsed['table']}
        return None

    @classmethod
    def _json_safe(cls, value):
        """Replaces float NaN / infinity with None, recursively. Python's json happily
        writes them as NaN/Infinity, which browsers cannot parse -- one stray NaN would
        break the whole widget."""
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        if isinstance(value, dict):
            return {key: cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._json_safe(item) for item in value]
        return value

    @staticmethod
    def _as_chart_payload(raw_content):
        """Returns the parsed {"chart": {...}} dict if `raw_content` is build_chart_from_
        file's JSON output, else None for every other tool's plain-text result. A
        plain `json.loads` that happens to succeed on some unrelated tool's text is not
        enough -- it must also have the "chart" key, which no other tool's output does.
        """
        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError):
            return None

        if isinstance(parsed, dict) and 'chart' in parsed:
            return parsed
        return None
