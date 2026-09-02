from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from document.models import Chunk, Document


class ChunkInline(TabularInline):
    model = Chunk
    fields = ['chunk_index', 'content']
    readonly_fields = ['chunk_index', 'content']
    extra = 0
    can_delete = False


@admin.register(Document)
class DocumentAdmin(ModelAdmin):
    list_display = ['id', 'title', 'company', 'doc_type', 'status', 'created_by', 'created_at']
    list_filter = ['status', 'doc_type', 'company']
    search_fields = ['title', 'source_url']
    inlines = [ChunkInline]

    def get_queryset(self, request):
        return Document.all_objects.all()


@admin.register(Chunk)
class ChunkAdmin(ModelAdmin):
    list_display = ['id', 'document', 'chunk_index', 'company']
    search_fields = ['content']

    def get_queryset(self, request):
        return Chunk.all_objects.all()
