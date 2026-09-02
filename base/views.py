from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError


class EnvelopeResponseMixin:
    def finalize_response(self, request, response, *args, **kwargs):
        if not response.exception and not (isinstance(response.data, dict) and 'success' in response.data):
            response.data = {
                'success': True,
                'message': 'Success',
                'data': response.data,
            }
        return super().finalize_response(request, response, *args, **kwargs)


class CompanyScopedMixin:
    def get_active_company(self, request):
        from company.models import Company
        from user.models import CompanyMembership

        company_id = request.query_params.get('company_id') or request.data.get('company_id')
        if not company_id:
            raise ValidationError('company_id is required.')

        try:
            company = Company.objects.get(id=company_id, is_active=True)
        except Company.DoesNotExist:
            raise NotFound('Company not found.')

        if not CompanyMembership.objects.filter(user=request.user, company=company).exists():
            raise PermissionDenied('You are not a member of this company.')

        return company
