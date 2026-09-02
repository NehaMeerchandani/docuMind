def is_full_admin(request):
    return bool(request.user.is_authenticated and request.user.is_superuser)
