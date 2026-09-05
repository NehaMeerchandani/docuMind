import os

from django.core.management.base import BaseCommand
from langfuse import Langfuse

from chat.services.prompt_service import FALLBACK_SYSTEM_PROMPT


class Command(BaseCommand):
    help = 'Creates/updates the chat prompts stored in LangFuse.'

    def handle(self, *args, **options):
        client = Langfuse(
            public_key=os.getenv('LANGFUSE_PUBLIC_KEY'),
            secret_key=os.getenv('LANGFUSE_SECRET_KEY'),
            host=os.getenv('LANGFUSE_BASE_URL'),
        )

        client.create_prompt(
            name='rag-system-prompt',
            prompt=FALLBACK_SYSTEM_PROMPT,
            labels=['production'],
        )

        self.stdout.write(self.style.SUCCESS('Synced rag-system-prompt to LangFuse.'))
