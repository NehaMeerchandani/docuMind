from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserCreationForm
from unfold.admin import ModelAdmin, TabularInline

from base.admin import AuditableAdminMixin
from user.models import CompanyMembership, CustomUser, RefreshToken


class CompanyMembershipInline(TabularInline):
    model = CompanyMembership
    fk_name = 'user'
    extra = 0


class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = CustomUser
        fields = ('username', 'email')

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = True
        if commit:
            user.save()
        return user


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin, ModelAdmin):
    inlines = [CompanyMembershipInline]
    list_display = ['id', 'username', 'email', 'is_staff', 'is_active']
    add_form = CustomUserCreationForm
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'password1', 'password2'),
        }),
    )

    def save_formset(self, request, form, formset, change):
        if formset.model is CompanyMembership:
            AuditableAdminMixin.save_formset(self, request, form, formset, change)
        else:
            super().save_formset(request, form, formset, change)


@admin.register(RefreshToken)
class RefreshTokenAdmin(AuditableAdminMixin, ModelAdmin):
    list_display = ['id', 'user', 'expires_at', 'is_revoked', 'created_at']
    list_filter = ['is_revoked']
    search_fields = ['user__email']

    fieldsets = (
        ('Details', {
            'classes': ('tab',),
            'fields': ('user', 'token', 'expires_at', 'is_revoked', 'is_active'),
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
