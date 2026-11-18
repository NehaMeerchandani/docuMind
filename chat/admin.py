import json
import mimetypes
import os

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib import admin
from django.core.files.storage import default_storage
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.utils.html import format_html
from django.shortcuts import render
from django.urls import path, reverse
from django.views import View
from unfold.admin import ModelAdmin, TabularInline

from base.active_company import get_active_company
from agents.models import Agent
from base.admin import AuditableAdminMixin
from chat.models import Conversation, Message
from chat.services.chat_service import ChatService
from document.services.parser_service import ParserService

ALLOWED_UPLOAD_EXTENSIONS = {'.csv', '.xlsx', '.xls', '.pdf', '.docx', '.txt', '.md'}


def _open_url(name):
    return reverse('admin:chat_media_file', kwargs={'name': name})


def media_file_view(request, name):
    """Opens one media file for a logged-in admin. Media is never served publicly."""
    try:
        if not default_storage.exists(name):
            raise Http404('File not found.')
        stored_file = default_storage.open(name, 'rb')
    except (SuspiciousFileOperation, ValueError, OSError):
        raise Http404('File not found.')
    content_type = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    return FileResponse(stored_file, content_type=content_type, filename=os.path.basename(name))


class HasMediaFilter(admin.SimpleListFilter):
    title = 'has media'
    parameter_name = 'has_media'

    def lookups(self, request, model_admin):
        return (('yes', 'With an uploaded file'), ('no', 'Without'))

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(media='')
        if self.value() == 'no':
            return queryset.filter(media='')
        return queryset


class MessageInline(TabularInline):
    model = Message
    fields = ['role', 'message_type', 'tool_name', 'message', 'created_at']
    readonly_fields = ['role', 'message_type', 'tool_name', 'message', 'created_at']
    extra = 0
    can_delete = False


def chat_interface_view(request):
    agents = [
        {
            'slug': agent.slug,
            'name': agent.name,
            'endpoint': reverse('agent-chat-stream', kwargs={'agent_slug': agent.slug}),
        }
        for agent in Agent.objects.filter(is_active=True)
    ]
    context = {
        'agents_json': json.dumps(agents),
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

    queryset = Conversation.objects.filter(user=request.user, company=company)
    agent_slug = request.GET.get('agent')
    if agent_slug:
        queryset = queryset.filter(agent__slug=agent_slug)
    conversations = list(
        queryset.values('session_id', 'title', 'created_at').order_by('-created_at'),
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
            agent_slug = request.POST.get('agent') or None
        else:
            body = json.loads(request.body or b'{}')
            question = body.get('question')
            session_id = body.get('session_id')
            agent_slug = body.get('agent') or None

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

        agent = None
        if agent_slug:
            agent = await Agent.objects.filter(slug=agent_slug, is_active=True).afirst()
            if agent is None:
                return JsonResponse({'success': False, 'error': 'Agent not found.', 'data': None}, status=404)

        if session_id:
            try:
                conversation = await Conversation.objects.aget(session_id=session_id, user=user)
            except Conversation.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'Conversation not found.', 'data': None}, status=404)
            if agent is not None and conversation.agent_id not in (None, agent.id):
                return JsonResponse(
                    {'success': False, 'error': 'This conversation belongs to a different agent.', 'data': None},
                    status=400,
                )
        else:
            conversation = await Conversation.objects.acreate(
                company=company, user=user, created_by=user, agent=agent,
            )

        response = StreamingHttpResponse(
            ChatService.stream_reply(conversation, company, question, attachment=attachment, agent=agent),
            content_type='text/event-stream',
        )
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        response['X-Session-Id'] = str(conversation.session_id)
        return response


@admin.register(Conversation)
class ConversationAdmin(AuditableAdminMixin, ModelAdmin):
    list_display = ['session_id', 'title', 'agent', 'company', 'user', 'created_at']
    list_filter = ['company', 'agent']
    search_fields = ['title', 'user__email']
    inlines = [MessageInline]

    fieldsets = (
        ('Details', {
            'classes': ('tab',),
            'fields': ('title', 'agent', 'company', 'user', 'summary', 'is_active'),
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
                'media-files/<path:name>',
                self.admin_site.admin_view(media_file_view),
                name='chat_media_file',
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
    list_display = ['id', 'conversation', 'role', 'message_type', 'media_link', 'created_at']
    list_filter = ['role', 'message_type', HasMediaFilter]
    search_fields = ['message']

    fieldsets = (
        ('Details', {
            'classes': ('tab',),
            'fields': ('conversation', 'role', 'message_type', 'message', 'media_link', 'is_active'),
        }),
        ('Audit info', {
            'classes': ('tab',),
            'fields': (
                'created_by', 'updated_by', 'created_at', 'updated_at',
                'is_deleted', 'deleted_at',
            ),
        }),
    )
    readonly_fields = ['media_link', 'created_at', 'updated_at']

    @admin.display(description='Uploaded file')
    def media_link(self, obj):
        if not obj.media:
            return '-'
        return format_html('<a href="{}" target="_blank">{}</a>', _open_url(obj.media.name), obj.media.name)

    def get_queryset(self, request):
        return Message.all_objects.all()
