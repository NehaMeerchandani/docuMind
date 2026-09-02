from rest_framework.permissions import BasePermission

from basic.constants.roles import CompanyRoles
from user.models import CompanyMembership


class IsUploaderOrCompanyAdmin(BasePermission):
    def has_object_permission(self, request, view, obj):
        if obj.created_by_id == request.user.id:
            return True

        return CompanyMembership.objects.filter(
            user=request.user,
            company=obj.company,
            role=CompanyRoles.ADMIN,
        ).exists()
