from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import path
from django.utils.http import url_has_allowed_host_and_scheme
from unfold.admin import ModelAdmin

from base.active_company import get_available_companies
from base.admin import AuditableAdminMixin
from company.models import Company


def set_active_company_view(request):
    if request.method == 'POST':
        company_id = request.POST.get('company_id')
        if company_id and get_available_companies(request.user).filter(id=company_id).exists():
            request.session['active_company_id'] = int(company_id)

    redirect_to = request.META.get('HTTP_REFERER', '')
    if not url_has_allowed_host_and_scheme(redirect_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        redirect_to = '/admin/'

    return HttpResponseRedirect(redirect_to)


@admin.register(Company)
class CompanyAdmin(AuditableAdminMixin, ModelAdmin):
    list_display = ['id', 'name', 'is_active', 'is_deleted', 'created_at']
    list_filter = ['is_active', 'is_deleted']
    search_fields = ['name']

    def get_queryset(self, request):
        return Company.all_objects.all()

    def get_urls(self):
        custom_urls = [
            path(
                'set-active/',
                self.admin_site.admin_view(set_active_company_view),
                name='set_active_company',
            ),
        ]
        return custom_urls + super().get_urls()
