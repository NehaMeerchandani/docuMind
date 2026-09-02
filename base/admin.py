class AuditableAdminMixin:
    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if instance.pk is None:
                instance.created_by = request.user
            instance.updated_by = request.user
            instance.save()
        formset.save_m2m()
