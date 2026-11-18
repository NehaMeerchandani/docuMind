from django.db import migrations

ADD = 'list_document_tasks'
REMOVE = 'list_document_process'  # replaced by the task table; stays registered for other agents


def swap_tools(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')
    agent = Agent.objects.filter(name='General Assistant').first()
    if agent is None:
        return

    for node in agent.workflow_json.get('nodes', []):
        tools = node.get('data', {}).get('tools')
        if node.get('type') == 'step' and tools is not None:
            if ADD not in tools:
                tools.append(ADD)
            if REMOVE in tools:
                tools.remove(REMOVE)
    agent.save(update_fields=['workflow_json'])


def restore_tools(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')
    agent = Agent.objects.filter(name='General Assistant').first()
    if agent is None:
        return

    for node in agent.workflow_json.get('nodes', []):
        tools = node.get('data', {}).get('tools')
        if node.get('type') == 'step' and tools is not None:
            if ADD in tools:
                tools.remove(ADD)
            if REMOVE not in tools:
                tools.append(REMOVE)
    agent.save(update_fields=['workflow_json'])


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0006_add_chat_document_tools'),
    ]

    operations = [
        migrations.RunPython(swap_tools, restore_tools),
    ]
