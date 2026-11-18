from django.db import migrations, models
from django.utils.text import slugify


def populate_slugs(apps, schema_editor):
    Agent = apps.get_model('agents', 'Agent')
    for agent in Agent.objects.all():
        agent.slug = slugify(agent.name)[:255]
        agent.save(update_fields=['slug'])


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0004_add_temporal_document_tools'),
    ]

    operations = [
        migrations.AddField(
            model_name='agent',
            name='slug',
            field=models.SlugField(max_length=255, null=True, editable=False, db_index=False),
        ),
        migrations.RunPython(populate_slugs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='agent',
            name='slug',
            field=models.SlugField(
                max_length=255, unique=True, editable=False,
                help_text=(
                    "URL-safe form of `name`, generated automatically. It identifies this agent in "
                    "its chat endpoint (/chat/agents/<slug>/stream/)."
                ),
            ),
        ),
    ]
