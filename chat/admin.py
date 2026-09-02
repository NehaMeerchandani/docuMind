import json

from asgiref.sync import sync_to_async
from django.contrib import admin
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import path
from django.views import View
from unfold.admin import ModelAdmin, TabularInline

from base.active_company import get_active_company
from base.admin import AuditableAdminMixin
from chat.models import Conversation, Message
from chat.services.chat_service import ChatService


class MessageInline(TabularInline):
    model = Message
    fields = ['sender', 'message_type', 'content', 'created_at']
    readonly_fields = ['sender', 'message_type', 'content', 'created_at']
    extra = 0
    can_delete = False


def chat_interface_view(request):
    context = {
        **admin.site.each_context(request),
        'active_company': get_active_company(request),
    }
    return render(request, 'admin/chat/chat_interface.html', context)


def conversation_list_view(request):
    company = get_active_company(request)
    if company is None:
        return JsonResponse({'success': True, 'error': None, 'data': []})

    conversations = list(
        Conversation.objects.filter(user=request.user, company=company)
        .values('session_id', 'title', 'created_at')
        .order_by('-created_at'),
    )
    return JsonResponse({'success': True, 'error': None, 'data': conversations})


def conversation_messages_view(request, session_id):
    try:
        conversation = Conversation.objects.get(session_id=session_id, user=request.user)
    except Conversation.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Conversation not found.', 'data': None}, status=404)

    messages = list(
        conversation.messages.values('id', 'sender', 'message_type', 'content', 'created_at'),
    )
    return JsonResponse({
        'success': True,
        'error': None,
        'data': {'title': conversation.title, 'messages': messages},
    })


class AdminChatStreamView(View):
    async def post(self, request):
        user = await request.auser()
        if not user.is_authenticated or not user.is_staff:
            return JsonResponse({'success': False, 'error': 'Staff login required.', 'data': None}, status=403)

        body = json.loads(request.body or b'{}')
        question = body.get('question')
        session_id = body.get('session_id')

        if not question:
            return JsonResponse(
                {'success': False, 'error': 'question is required.', 'data': None},
                status=400,
            )

        company = await sync_to_async(get_active_company)(request)
        if company is None:
            return JsonResponse(
                {'success': False, 'error': 'Select a company from the top-right dropdown first.', 'data': None},
                status=400,
            )

        if session_id:
            try:
                conversation = await Conversation.objects.aget(session_id=session_id, user=user)
            except Conversation.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'Conversation not found.', 'data': None}, status=404)
        else:
            conversation = await Conversation.objects.acreate(company=company, user=user, created_by=user)

        response = StreamingHttpResponse(
            ChatService.stream_reply(conversation, company, question),
            content_type='text/event-stream',
        )
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        response['X-Session-Id'] = str(conversation.session_id)
        return response


@admin.register(Conversation)
class ConversationAdmin(AuditableAdminMixin, ModelAdmin):
    list_display = ['session_id', 'title', 'user', 'company', 'created_at']
    list_filter = ['company']
    search_fields = ['title', 'user__email']
    inlines = [MessageInline]

    def get_queryset(self, request):
        return Conversation.all_objects.all()

    def get_urls(self):
        custom_urls = [
            path(
                'chat-interface/',
                self.admin_site.admin_view(chat_interface_view),
                name='chat_interface',
            ),
            path(
                'chat-interface/stream/',
                AdminChatStreamView.as_view(),
                name='chat_interface_stream',
            ),
            path(
                'chat-interface/conversations/',
                self.admin_site.admin_view(conversation_list_view),
                name='chat_interface_conversations',
            ),
            path(
                'chat-interface/conversations/<uuid:session_id>/messages/',
                self.admin_site.admin_view(conversation_messages_view),
                name='chat_interface_conversation_messages',
            ),
        ]
        return custom_urls + super().get_urls()


@admin.register(Message)
class MessageAdmin(AuditableAdminMixin, ModelAdmin):
    list_display = ['id', 'conversation', 'sender', 'message_type', 'created_at']
    list_filter = ['sender', 'message_type']
    search_fields = ['content']

    def get_queryset(self, request):
        return Message.all_objects.all()
