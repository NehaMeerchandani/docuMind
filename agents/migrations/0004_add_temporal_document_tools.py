from django.db import migrations

TOOLS = ['process_document', 'get_document_status', 'control_document_processing']


def add_tools(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')
    agent = Agent.objects.filter(name='General Assistant').first()
    if agent is None:
        return

    for node in agent.workflow_json.get('nodes', []):
        tools = node.get('data', {}).get('tools')
        if node.get('type') == 'step' and tools is not None:
            tools.extend(tool for tool in TOOLS if tool not in tools)
    agent.save(update_fields=['workflow_json'])


def remove_tools(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')
    agent = Agent.objects.filter(name='General Assistant').first()
    if agent is None:
        return

    for node in agent.workflow_json.get('nodes', []):
        tools = node.get('data', {}).get('tools')
        if tools:
            tools[:] = [tool for tool in tools if tool not in TOOLS]
    agent.save(update_fields=['workflow_json'])


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0003_add_summarize_document_tool'),
    ]

    operations = [
        migrations.RunPython(add_tools, remove_tools),
    ]
