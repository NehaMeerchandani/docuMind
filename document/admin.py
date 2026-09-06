from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from base.active_company import get_active_company
from base.admin import AuditableAdminMixin
from document.models import Chunk, Document, DocumentStatus
from document.services.document_service import DocumentService
from document.services.kafka_service import KafkaService
from document.tasks import process_document_task


class ChunkInline(TabularInline):
    model = Chunk
    fields = ['chunk_index', 'content']
    readonly_fields = ['chunk_index', 'content']
    extra = 0
    can_delete = False


@admin.register(Document)
class DocumentAdmin(AuditableAdminMixin, ModelAdmin):
    list_display = [
        'id', 'title', 'company', 'doc_type', 'status_badge', 'chunk_count',
        'created_by', 'created_at',
        'process_link', 'process_celery_link', 'process_kafka_link',
    ]
    list_filter = ['status', 'doc_type']
    search_fields = ['title', 'source_url']
    inlines = [ChunkInline]

    fieldsets = (
        ('Details', {
            'classes': ('tab',),
            'fields': ('title', 'source_url', 'content', 'doc_type', 'status', 'error_message', 'is_active'),
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

    class Media:
        css = {'all': ['admin/css/document_process.css']}
        js = ['admin/js/document_process.js']

    def get_urls(self):
        # require_POST is applied here, to the already-bound method (self.process_...),
        # not as a decorator on the method definition itself. Decorating the method
        # definition directly would make require_POST inspect `self` as if it were the
        # request (since that's the first positional arg at definition time), raising
        # AttributeError: 'DocumentAdmin' object has no attribute 'method'. Once bound,
        # the method's effective signature is (request, object_id), which is what
        # require_POST expects.
        custom_urls = [
            path(
                '<int:object_id>/process/',
                self.admin_site.admin_view(require_POST(self.process_document_view)),
                name='document_document_process',
            ),
            path(
                '<int:object_id>/process-celery/',
                self.admin_site.admin_view(require_POST(self.process_document_celery_view)),
                name='document_document_process_celery',
            ),
            path(
                '<int:object_id>/process-kafka/',
                self.admin_site.admin_view(require_POST(self.process_document_kafka_view)),
                name='document_document_process_kafka',
            ),
            path(
                '<int:object_id>/status/',
                self.admin_site.admin_view(self.document_status_view),
                name='document_document_status',
            ),
        ]
        return custom_urls + super().get_urls()

    @display(description='Status')
    def status_badge(self, obj):
        # Rendered by hand (rather than @display(label=...)) so the badge carries
        # data-status-cell -- document_process.js finds and rewrites this element in
        # place (same doc-status-badge--<status> class naming) once a background
        # process finishes, without a page reload.
        return format_html(
            '<span data-status-cell class="doc-status-badge doc-status-badge--{}">{}</span>',
            obj.status,
            obj.get_status_display(),
        )

    @display(description='Chunks')
    def chunk_count(self, obj):
        return obj.chunks.count()

    def _process_button(self, obj, *, url_name, css_variant, label, retry_label):
        """Shared renderer for the three process buttons (sync/Celery/Kafka).

        Buttons carry data attributes only -- no href navigation. document_process.js
        reads these to fire an AJAX POST and update this row in place, so clicking
        never reloads the page.
        """
        if obj.status == DocumentStatus.COMPLETED:
            return '-'

        if not self._can_manage(self.request, obj):
            return format_html(
                '<span title="Only the uploader or an admin can process this document." '
                'style="color:#9ca3af;font-size:12px;">Not your document</span>',
            )

        button_label = retry_label if obj.status in (DocumentStatus.PROCESSING, DocumentStatus.FAILED) else label
        url = reverse(url_name, args=[obj.pk])
        status_url = reverse('admin:document_document_status', args=[obj.pk])
        disabled = obj.status == DocumentStatus.PROCESSING

        return format_html(
            '<button type="button" class="doc-process-btn doc-process-btn--{}" '
            'data-process-url="{}" data-status-url="{}" data-document-id="{}"{}>{}</button>',
            css_variant,
            url,
            status_url,
            obj.pk,
            mark_safe(' disabled') if disabled else '',
            button_label,
        )

    @display(description='Process')
    def process_link(self, obj):
        return self._process_button(
            obj,
            url_name='admin:document_document_process',
            css_variant='sync',
            label='Process',
            retry_label='Retry',
        )

    @display(description='Celery')
    def process_celery_link(self, obj):
        return self._process_button(
            obj,
            url_name='admin:document_document_process_celery',
            css_variant='celery',
            label='Process',
            retry_label='Retry',
        )

    @display(description='Kafka')
    def process_kafka_link(self, obj):
        return self._process_button(
            obj,
            url_name='admin:document_document_process_kafka',
            css_variant='kafka',
            label='Process',
            retry_label='Retry',
        )

    def process_document_view(self, request, object_id):
        document = Document.all_objects.get(pk=object_id)

        if not self._can_manage(request, document):
            return JsonResponse(
                {'success': False, 'error': "You don't have permission to process this document."},
                status=403,
            )

        try:
            DocumentService.process(document, user=request.user)
            return JsonResponse({'success': True, 'message': 'Document processing has started.'})
        except Exception as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    def process_document_celery_view(self, request, object_id):
        document = Document.all_objects.get(pk=object_id)

        if not self._can_manage(request, document):
            return JsonResponse(
                {'success': False, 'error': "You don't have permission to process this document."},
                status=403,
            )

        # .delay(...) does NOT run the task now -- it serializes these arguments to JSON
        # and pushes them onto the Redis queue, then returns immediately. The task only
        # actually executes once a separate `celery -A main worker` process picks it up.
        process_document_task.delay(document.pk, request.user.pk)
        return JsonResponse({'success': True, 'message': 'Document processing has started.'})

    def process_document_kafka_view(self, request, object_id):
        document = Document.all_objects.get(pk=object_id)

        if not self._can_manage(request, document):
            return JsonResponse(
                {'success': False, 'error': "You don't have permission to process this document."},
                status=403,
            )

        KafkaService.publish_document_processing_event(document.pk, request.user.pk)
        return JsonResponse({'success': True, 'message': 'Document queued for processing.'})

    def document_status_view(self, request, object_id):
        """Polled by document_process.js after a process button is clicked, until the
        document's status leaves PROCESSING (i.e. reaches COMPLETED or FAILED)."""
        try:
            document = Document.all_objects.get(pk=object_id)
        except Document.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Document not found.'}, status=404)

        return JsonResponse({
            'success': True,
            'status': document.status,
            'status_display': document.get_status_display(),
            'chunk_count': document.chunks.count(),
            'error_message': document.error_message,
        })


@admin.register(Chunk)
class ChunkAdmin(ModelAdmin):
    list_display = ['id', 'document', 'company', 'chunk_index']
    search_fields = ['content']

    fieldsets = (
        ('Details', {
            'classes': ('tab',),
            'fields': ('document', 'company', 'chunk_index', 'content', 'is_active'),
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
