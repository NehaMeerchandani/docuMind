from django.db import migrations

TOOL = 'summarize_document'


def add_tool(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')
    agent = Agent.objects.filter(name='General Assistant').first()
    if agent is None:
        return

    for node in agent.workflow_json.get('nodes', []):
        tools = node.get('data', {}).get('tools')
        if node.get('type') == 'step' and tools is not None and TOOL not in tools:
            tools.append(TOOL)
    agent.save(update_fields=['workflow_json'])


def remove_tool(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')
    agent = Agent.objects.filter(name='General Assistant').first()
    if agent is None:
        return

    for node in agent.workflow_json.get('nodes', []):
        tools = node.get('data', {}).get('tools')
        if tools and TOOL in tools:
            tools.remove(TOOL)
    agent.save(update_fields=['workflow_json'])


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0002_seed_default_agent'),
    ]

    operations = [
        migrations.RunPython(add_tool, remove_tool),
    ]
