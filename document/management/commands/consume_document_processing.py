import json

from confluent_kafka import Consumer
from django.core.management.base import BaseCommand

from document.models import Document
from document.services.document_service import DocumentService
from document.services.kafka_service import KafkaService
from user.models import CustomUser


class Command(BaseCommand):
    help = (
        'Runs a long-lived Kafka consumer that subscribes to the document-processing '
        'topic and calls DocumentService.process for every event received. '
        'Leave this running in its own terminal, similar to the Celery worker.'
    )

    def handle(self, *args, **options):
        consumer = Consumer({
            'bootstrap.servers': KafkaService.BOOTSTRAP_SERVERS,
            # A consumer group is how Kafka tracks "which messages has this logical
            # consumer already seen" -- if we ever ran two copies of this command at
            # once with the same group id, Kafka would split the topic's messages
            # between them instead of both processing every message.
            'group.id': 'document-processing-consumer',
            # Only relevant the very first time this consumer group connects (i.e. no
            # committed offset exists yet): start from the beginning of the topic
            # rather than only seeing events published after this line ran.
            'auto.offset.reset': 'earliest',
            # Disable auto-commit -- we commit manually, only after DocumentService.process
            # actually finishes, so a crash mid-processing means this message gets
            # redelivered on restart instead of silently being marked as done.
            'enable.auto.commit': False,
        })
        consumer.subscribe([KafkaService.DOCUMENT_PROCESSING_TOPIC])

        self.stdout.write(self.style.SUCCESS(
            f'Listening on topic "{KafkaService.DOCUMENT_PROCESSING_TOPIC}"... (Ctrl+C to stop)',
        ))

        try:
            while True:
                # poll() blocks for up to 1 second waiting for a new message. Returning
                # None just means "nothing arrived in that second" -- not an error --
                # so the loop simply tries again.
                message = consumer.poll(timeout=1.0)

                if message is None:
                    continue

                if message.error():
                    self.stderr.write(self.style.ERROR(f'Kafka error: {message.error()}'))
                    continue

                self._handle_message(message)
                consumer.commit(message)
        except KeyboardInterrupt:
            self.stdout.write('Stopping consumer...')
        finally:
            consumer.close()

    def _handle_message(self, message):
        # Everything here is inside one try/except, including the JSON parsing itself.
        # A single bad/unexpected message (e.g. something not published by our own
        # KafkaService, or manual test traffic sent while debugging) must never be able
        # to crash this whole long-running process -- it should be logged and skipped,
        # with the consumer moving on to the next message.
        try:
            payload = json.loads(message.value())
            document_id = payload['document_id']
            user_id = payload.get('user_id')

            self.stdout.write(f'Received event for document_id={document_id}')

            document = Document.all_objects.get(pk=document_id)
            user = CustomUser.objects.get(pk=user_id) if user_id else None
            DocumentService.process(document, user=user)
            self.stdout.write(self.style.SUCCESS(f'Processed document_id={document_id}'))
        except Exception as exc:
            # DocumentService.process already saves status=FAILED + error_message on the
            # document itself for any failure inside its own try/except. This outer layer
            # also catches errors that happen outside DocumentService.process entirely --
            # malformed/non-JSON messages, a missing document_id key, Document.DoesNotExist,
            # or a database error -- anything that would otherwise crash this process.
            self.stderr.write(self.style.ERROR(f'Failed to handle message: {exc}'))
