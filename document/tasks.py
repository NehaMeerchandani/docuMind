from celery import shared_task

from document.models import Document
from document.services.document_service import DocumentService
from user.models import CustomUser


@shared_task(bind=True)
def process_document_task(self, document_id, user_id=None):
    """Celery task wrapper around DocumentService.process.

    Runs in a separate Celery worker process, not in the Django request/response
    cycle -- so it can't be handed live Python objects like `request.user`. Only
    plain, JSON-serializable values (ids) get passed in; the objects are re-fetched
    here, inside the worker.
    """
    document = Document.all_objects.get(pk=document_id)
    user = CustomUser.objects.get(pk=user_id) if user_id else None

    DocumentService.process(document, user=user)

    return {'document_id': document_id, 'task_id': self.request.id}
