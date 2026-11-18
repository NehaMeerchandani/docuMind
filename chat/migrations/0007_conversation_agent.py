import django.db.models.deletion
from django.db import migrations, models

DEFAULT_AGENT_NAME = 'General Assistant'


def assign_default_agent(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')
    Conversation = apps.get_model('chat', 'Conversation')
    default_agent = Agent.objects.filter(name=DEFAULT_AGENT_NAME).first()
    if default_agent is not None:
        Conversation.objects.filter(agent__isnull=True).update(agent=default_agent)


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0005_agent_slug'),
        ('chat', '0006_delete_documentwatch'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='agent',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='conversations', to='agents.agent',
                help_text='The agent this conversation belongs to; every turn is answered by it.',
            ),
        ),
        migrations.RunPython(assign_default_agent, migrations.RunPython.noop),
    ]
