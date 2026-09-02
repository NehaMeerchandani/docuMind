def get_available_companies(user):
    from company.models import Company
    from user.models import CompanyMembership

    if user.is_superuser:
        return Company.objects.filter(is_active=True)

    company_ids = CompanyMembership.objects.filter(user=user).values_list('company_id', flat=True)
    return Company.objects.filter(id__in=company_ids, is_active=True)


def get_active_company(request):
    user = request.user
    if not user.is_authenticated:
        return None

    companies = get_available_companies(user)
    active_id = request.session.get('active_company_id')

    if active_id:
        company = companies.filter(id=active_id).first()
        if company:
            return company

    company = companies.first()
    if company:
        request.session['active_company_id'] = company.id
    return company
