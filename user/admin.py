from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from unfold.admin import ModelAdmin, TabularInline

from user.models import CompanyMembership, CustomUser, RefreshToken


class CompanyMembershipInline(TabularInline):
    model = CompanyMembership
    fk_name = 'user'
    extra = 0


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin, ModelAdmin):
    inlines = [CompanyMembershipInline]
    list_display = ['id', 'username', 'email', 'is_staff', 'is_active']


@admin.register(RefreshToken)
class RefreshTokenAdmin(ModelAdmin):
    list_display = ['id', 'user', 'is_revoked', 'expires_at', 'created_at']
    list_filter = ['is_revoked']
    search_fields = ['user__email']
