import asyncio
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.management.base import BaseCommand
from temporalio.client import Client
from temporalio.worker import Worker

from document.temporal.activities import ALL_ACTIVITIES
from document.temporal.workflows import ChatDocumentWorkflow, ProcessDocumentWorkflow

MAX_CONCURRENT_ACTIVITIES = 8


class Command(BaseCommand):
    help = (
        'Runs the Temporal worker for document processing: it polls the '
        f'"{settings.TEMPORAL_TASK_QUEUE}" task queue and executes ProcessDocumentWorkflow, '
        'ChatDocumentWorkflow and their activities. Start the Temporal server first (see INFRA_SETUP_GUIDE.md).'
    )

    def handle(self, *args, **options):
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            self.stdout.write('Worker stopped.')

    async def _run(self):
        client = await Client.connect(settings.TEMPORAL_SERVER_URL, namespace=settings.TEMPORAL_NAMESPACE)

        # Activities are sync (Django ORM/requests are blocking), so they run in threads.
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ACTIVITIES) as executor:
            worker = Worker(
                client,
                task_queue=settings.TEMPORAL_TASK_QUEUE,
                workflows=[ProcessDocumentWorkflow, ChatDocumentWorkflow],
                activities=ALL_ACTIVITIES,
                activity_executor=executor,
                max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
            )
            self.stdout.write(self.style.SUCCESS(
                f'Temporal worker running on queue "{settings.TEMPORAL_TASK_QUEUE}" '
                f'({settings.TEMPORAL_SERVER_URL}). Ctrl+C to stop.'
            ))
            await worker.run()
