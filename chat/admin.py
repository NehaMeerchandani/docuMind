import json
import os

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib import admin
from django.core.files.storage import default_storage
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import path
from django.views import View
from unfold.admin import ModelAdmin, TabularInline

from base.active_company import get_active_company
from base.admin import AuditableAdminMixin
from chat.models import Conversation, Message
from chat.services.chat_service import ChatService
from document.services.parser_service import ParserService

ALLOWED_UPLOAD_EXTENSIONS = {'.csv', '.xlsx', '.xls', '.pdf', '.docx', '.txt', '.md'}


class MessageInline(TabularInline):
    model = Message
    fields = ['role', 'message_type', 'tool_name', 'message', 'created_at']
    readonly_fields = ['role', 'message_type', 'tool_name', 'message', 'created_at']
    extra = 0
    can_delete = False


def chat_interface_view(request):
    context = {
        **admin.site.each_context(request),
        'active_company': get_active_company(request),
        # Steers which transport static/admin/js/chat.js uses (see main/settings.py) --
        # both the SSE endpoint below and the ws/chat/<session_id>/ consumer
        # (chat/consumers.py) are always live regardless of this value.
        'chat_transport': settings.CHAT_TRANSPORT,
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
        conversation.messages.values('id', 'role', 'message_type', 'message', 'tool_name', 'created_at'),
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

        # A file-attach turn arrives as multipart/form-data (question/session_id as
        # regular form fields, plus a `file` part) instead of the original
        # application/json body -- request.FILES is only ever populated for the
        # former, so that's what decides which body we parse. The plain-JSON path is
        # completely unchanged for every message that doesn't attach a file.
        uploaded_file = request.FILES.get('file')
        if uploaded_file is not None:
            question = request.POST.get('question')
            session_id = request.POST.get('session_id') or None
        else:
            body = json.loads(request.body or b'{}')
            question = body.get('question')
            session_id = body.get('session_id')

        if not question:
            return JsonResponse(
                {'success': False, 'error': 'question is required.', 'data': None},
                status=400,
            )

        attachment = None
        if uploaded_file is not None:
            extension = os.path.splitext(uploaded_file.name)[1].lower()
            if extension not in ALLOWED_UPLOAD_EXTENSIONS:
                return JsonResponse(
                    {
                        'success': False,
                        'error': f'Unsupported file type "{extension}". Allowed: '
                                 f'{", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}.',
                        'data': None,
                    },
                    status=400,
                )
            if uploaded_file.size > ParserService.MAX_CONTENT_BYTES:
                return JsonResponse(
                    {'success': False, 'error': 'File is too large (max 20 MB).', 'data': None},
                    status=400,
                )

            # Saved first, and the name storage RETURNS is what's kept: on a name clash
            # it differs from the requested one (report_x7k2.pdf), and that final path is
            # what later steps (summary, chart, Document.file) use to find the file.
            stored_path = await sync_to_async(default_storage.save)(
                f'chat_uploads/{uploaded_file.name}', uploaded_file,
            )
            attachment = {'filename': uploaded_file.name, 'stored_path': stored_path}

            # Nothing is parsed at upload time. Summary, processing and charts each read
            # the stored file only when the user actually asks for them.

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
            ChatService.stream_reply(conversation, company, question, attachment=attachment),
            content_type='text/event-stream',
        )
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        response['X-Session-Id'] = str(conversation.session_id)
        return response


@admin.register(Conversation)
class ConversationAdmin(AuditableAdminMixin, ModelAdmin):
    list_display = ['session_id', 'title', 'company', 'user', 'created_at']
    list_filter = ['company']
    search_fields = ['title', 'user__email']
    inlines = [MessageInline]

    fieldsets = (
        ('Details', {
            'classes': ('tab',),
            'fields': ('title', 'company', 'user', 'summary', 'is_active'),
        }),
        ('Audit info', {
            'classes': ('tab',),
            'fields': (
                'created_by', 'updated_by', 'created_at', 'updated_at',
                'is_deleted', 'deleted_at',
            ),
        }),
    )
    readonly_fields = ['created_at', 'updated_at']

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
    list_display = ['id', 'conversation', 'role', 'message_type', 'tool_name', 'created_at']
    list_filter = ['role', 'message_type']
    search_fields = ['message']

    fieldsets = (
        ('Details', {
            'classes': ('tab',),
            'fields': ('conversation', 'role', 'message_type', 'tool_name', 'message', 'is_active'),
        }),
        ('Audit info', {
            'classes': ('tab',),
            'fields': (
                'created_by', 'updated_by', 'created_at', 'updated_at',
                'is_deleted', 'deleted_at',
            ),
        }),
    )
    readonly_fields = ['created_at', 'updated_at']

    def get_queryset(self, request):
        return Message.all_objects.all()
