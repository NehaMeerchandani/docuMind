import json

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from base.active_company import get_available_companies
from chat.models import Conversation
from chat.services.chat_service import ChatService


class ChatConsumer(AsyncWebsocketConsumer):
    """WebSocket counterpart to the admin chat interface's AdminChatStreamView
    (chat/admin.py).

    This mirrors the *admin* chat interface specifically (session-cookie + is_staff
    auth, active company picked from the session, like chat/admin.py's
    chat_interface_view) -- not chat/views.py's JWT-authenticated REST API. Both the
    SSE endpoint (chat/admin.py) and this consumer call the exact same
    ChatService.stream_reply generator, so the LLM/agent/tool logic is never
    duplicated -- this class only adapts that generator's SSE-formatted output into
    WebSocket text frames.

    Connecting also joins a per-user channel-layer group (`user_<user_id>`) -- not a
    per-conversation one. This is deliberate: a document-processing notification is
    about who *uploaded* that document (document.created_by), not about which
    conversation happened to ask "what's pending" -- so DocumentService.process() (see
    document/services/document_service.py) looks up the uploader's group directly and
    pushes there, reaching this user on whichever conversation(s) they currently have
    open, the instant their document finishes. See document_notification() below.

    One socket = one conversation (for the chat-message part of this consumer), but the
    notification group is shared across every conversation this same user has open --
    a user with two tabs open on two different conversations gets the notification in
    both, since both sockets joined the same `user_<user_id>` group.

    The frontend picks the session_id (a UUID) itself before connecting -- for a brand
    new chat it generates a fresh UUID client-side (see static/admin/js/chat.js),
    rather than the server minting one as the SSE flow does, since the WebSocket URL
    needs the id upfront to route the connection.
    """

    async def connect(self):
        self.session_id = self.scope['url_route']['kwargs']['session_id']
        self.conversation = None
        self.company = None
        self.user_group_name = None

        user = self.scope.get('user')
        if user is None or not user.is_authenticated or not user.is_staff:
            await self.close(code=4401)  # unauthorized
            return

        active_company_id = self.scope['session'].get('active_company_id')
        self.company = await self._resolve_company(user, active_company_id)
        if self.company is None:
            await self.close(code=4400)  # no company available/selected
            return

        self.user = user
        self.conversation = await self._get_or_create_conversation()
        if self.conversation is None:
            await self.close(code=4404)  # session_id belongs to a different user
            return

        self.user_group_name = f'user_{self.user.id}'
        await self.channel_layer.group_add(self.user_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if self.user_group_name:
            await self.channel_layer.group_discard(self.user_group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({'error': 'Invalid JSON.'}))
            return

        question = payload.get('question')
        if not question:
            await self.send(text_data=json.dumps({'error': 'question is required.'}))
            return

        async for sse_chunk in ChatService.stream_reply(self.conversation, self.company, question):
            await self._forward_sse_chunk(sse_chunk)

    async def _forward_sse_chunk(self, sse_chunk):
        """ChatService.stream_reply yields SSE-framed strings ("data: {...}\\n\\n" or
        "data: [DONE]\\n\\n") so the exact same generator also works unmodified for the
        HTTP SSE endpoint. Here we just unwrap that framing into a plain JSON text frame
        per WebSocket message -- no [DONE] sentinel string over the wire, a proper
        {"done": true} object instead.
        """
        if not sse_chunk.startswith('data: '):
            return

        raw_payload = sse_chunk[len('data: '):].strip()
        if raw_payload == '[DONE]':
            await self.send(text_data=json.dumps({'done': True}))
            return

        await self.send(text_data=raw_payload)

    async def document_notification(self, event):
        """Channel-layer group handler -- invoked when DocumentService.process()
        (document/services/document_service.py) sends a {'type': 'document.notification',
        'message': {...}} event to this conversation's group after a watched document
        finishes. Channels maps 'document.notification' -> this method by convention
        (dots become underscores)."""
        await self.send(text_data=json.dumps({'document_notification': event['message']}))

    @staticmethod
    @sync_to_async
    def _resolve_company(user, active_company_id):
        companies = get_available_companies(user)
        if active_company_id:
            company = companies.filter(id=active_company_id).first()
            if company:
                return company
        return companies.first()

    @sync_to_async
    def _get_or_create_conversation(self):
        conversation, _created = Conversation.objects.get_or_create(
            session_id=self.session_id,
            defaults={'company': self.company, 'user': self.user, 'created_by': self.user},
        )
        if conversation.user_id != self.user.id:
            return None
        return conversation
