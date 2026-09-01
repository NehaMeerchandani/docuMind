from django.contrib import admin
from unfold.admin import ModelAdmin

from company.models import Company


@admin.register(Company)
class CompanyAdmin(ModelAdmin):
    list_display = ['id', 'name', 'is_active', 'is_deleted', 'created_at']
    list_filter = ['is_active', 'is_deleted']
    search_fields = ['name']

    def get_queryset(self, request):
        return Company.all_objects.all()
