from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from base.active_company import get_active_company
from base.admin import AuditableAdminMixin
from document.models import Chunk, Document, DocumentStatus
from document.services.document_service import DocumentService


class ChunkInline(TabularInline):
    model = Chunk
    fields = ['chunk_index', 'content']
    readonly_fields = ['chunk_index', 'content']
    extra = 0
    can_delete = False


@admin.register(Document)
class DocumentAdmin(AuditableAdminMixin, ModelAdmin):
    list_display = [
        'id', 'title', 'company', 'doc_type', 'status_badge',
        'chunk_count', 'process_link', 'created_by', 'created_at',
    ]
    list_filter = ['status', 'doc_type']
    search_fields = ['title', 'source_url']
    inlines = [ChunkInline]
    exclude = ['company']

    def get_queryset(self, request):
        self.request = request
        company = get_active_company(request)
        if company is None:
            return Document.all_objects.none()
        return Document.all_objects.filter(company=company)

    def has_module_permission(self, request):
        return bool(request.user.is_active and request.user.is_staff)

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_staff)

    def has_add_permission(self, request):
        return bool(request.user.is_active and request.user.is_staff and get_active_company(request))

    @staticmethod
    def _can_manage(request, obj):
        return bool(request.user.is_superuser or obj.created_by_id == request.user.id)

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return bool(request.user.is_active and request.user.is_staff)
        return self._can_manage(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is None:
            return bool(request.user.is_active and request.user.is_staff)
        return self._can_manage(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.company = get_active_company(request)
        super().save_model(request, obj, form, change)

    def get_urls(self):
        custom_urls = [
            path(
                '<int:object_id>/process/',
                self.admin_site.admin_view(self.process_document_view),
                name='document_document_process',
            ),
        ]
        return custom_urls + super().get_urls()

    @display(
        description='Status',
        label={
            DocumentStatus.PENDING: 'warning',
            DocumentStatus.PROCESSING: 'info',
            DocumentStatus.COMPLETED: 'success',
            DocumentStatus.FAILED: 'danger',
        },
    )
    def status_badge(self, obj):
        return obj.status

    @display(description='Chunks')
    def chunk_count(self, obj):
        return obj.chunks.count()

    @display(description='Action')
    def process_link(self, obj):
        if obj.status == DocumentStatus.COMPLETED:
            return '-'

        if not self._can_manage(self.request, obj):
            return format_html(
                '<span title="Only the uploader or an admin can process this document." '
                'style="color:#9ca3af;font-size:12px;">Not your document</span>',
            )

        label = 'Process' if obj.status == DocumentStatus.PENDING else 'Retry'
        url = reverse('admin:document_document_process', args=[obj.pk])
        return format_html(
            '<a href="{}" style="display:inline-block;background:rgb(147 51 234);color:#fff;'
            'padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;'
            'text-decoration:none;white-space:nowrap;">{}</a>',
            url,
            label,
        )

    def process_document_view(self, request, object_id):
        document = Document.all_objects.get(pk=object_id)

        if not self._can_manage(request, document):
            self.message_user(
                request,
                'You have not uploaded this document and are not an admin, so you cannot process it.',
                level=messages.ERROR,
            )
            return HttpResponseRedirect(reverse('admin:document_document_changelist'))

        try:
            DocumentService.process(document, user=request.user)
            self.message_user(request, f'"{document}" processed successfully.', level=messages.SUCCESS)
        except Exception as exc:
            self.message_user(request, f'Failed to process "{document}": {exc}', level=messages.ERROR)

        return HttpResponseRedirect(reverse('admin:document_document_changelist'))


@admin.register(Chunk)
class ChunkAdmin(ModelAdmin):
    list_display = ['id', 'document', 'chunk_index', 'company']
    search_fields = ['content']

    def get_queryset(self, request):
        company = get_active_company(request)
        if company is None:
            return Chunk.all_objects.none()
        return Chunk.all_objects.filter(company=company)

    def has_module_permission(self, request):
        return bool(request.user.is_active and request.user.is_staff)

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_staff)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_superuser)
