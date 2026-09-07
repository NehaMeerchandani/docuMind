from django.db import migrations

DEFAULT_WORKFLOW_JSON = {
    'nodes': [
        {'id': 'start-1', 'type': 'start', 'position': {'x': 40, 'y': 120}},
        {
            'id': 'step-1',
            'type': 'step',
            'position': {'x': 320, 'y': 120},
            'data': {
                'name': 'rag-system-prompt',
                'tools': ['search_documents', 'summarize_session'],
            },
        },
    ],
    'edges': [
        {'id': 'edge-1', 'source': 'start-1', 'target': 'step-1'},
    ],
}

DEFAULT_AGENT_NAME = 'General Assistant'
DEFAULT_AGENT_DESCRIPTION = (
    "The default, general-purpose company assistant. Handles document questions, "
    "session summaries, and general knowledge questions -- pick this unless a message "
    "clearly matches a more specific agent's description."
)


def seed_default_agent(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')

    # This migration reproduces, exactly, what chat_service.py hardcoded before the
    # dynamic workflow system existed: one agent, one step, prompt slug
    # 'rag-system-prompt' (the same Langfuse prompt already in production), bound to
    # the same two tools. Existing conversations/behavior see no change.
    Agent.objects.get_or_create(
        name=DEFAULT_AGENT_NAME,
        defaults={
            'description': DEFAULT_AGENT_DESCRIPTION,
            'workflow_json': DEFAULT_WORKFLOW_JSON,
        },
    )


def remove_default_agent(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')
    Agent.objects.filter(name=DEFAULT_AGENT_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_default_agent, remove_default_agent),
    ]
