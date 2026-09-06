import json
import os

from confluent_kafka import Producer


class KafkaService:
    BOOTSTRAP_SERVERS = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
    DOCUMENT_PROCESSING_TOPIC = os.getenv('KAFKA_DOCUMENT_PROCESSING_TOPIC', 'document-processing')

    _producer = None

    @classmethod
    def get_producer(cls):
        if cls._producer is None:
            cls._producer = Producer({'bootstrap.servers': cls.BOOTSTRAP_SERVERS})
        return cls._producer

    @classmethod
    def publish_document_processing_event(cls, document_id, user_id=None):
        """Publish a 'process this document' event to Kafka.

        Unlike Celery's .delay(), this does not hand off to a task queue that
        directly executes our code. It only appends a message to the topic's log --
        any process subscribed as a consumer (see document/management/commands/
        consume_document_processing.py) decides independently whether/how to act on
        it. produce() itself is non-blocking (the message is buffered locally and
        sent in the background); flush() blocks until the broker has actually
        acknowledged it, so callers know for certain the event was not lost before
        this function returns.
        """
        producer = cls.get_producer()
        payload = json.dumps({'document_id': document_id, 'user_id': user_id})

        producer.produce(
            cls.DOCUMENT_PROCESSING_TOPIC,
            key=str(document_id),
            value=payload,
        )
        producer.flush()
