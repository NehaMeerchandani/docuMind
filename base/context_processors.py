from base.active_company import get_active_company, get_available_companies


def active_company_context(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated or not user.is_staff:
        return {}

    return {
        'active_company': get_active_company(request),
        'available_companies': get_available_companies(user),
    }
