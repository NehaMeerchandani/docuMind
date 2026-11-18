import json

from django.db import migrations, models


def backfill_media(apps, schema_editor):
    """Existing chat uploads were only recorded in CONTEXT rows' JSON; copy each path to
    the new `media` field so they show up in the admin."""
    Message = apps.get_model('chat', 'Message')
    for message in Message.objects.filter(message_type='context', media=''):
        try:
            payload = json.loads(message.message or '')
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get('kind') == 'attachment_file' and payload.get('stored_path'):
            message.media = payload['stored_path']
            message.save(update_fields=['media'])


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0007_conversation_agent'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='media',
            field=models.FileField(blank=True, max_length=255, upload_to='chat_uploads/'),
        ),
        migrations.RunPython(backfill_media, migrations.RunPython.noop),
    ]
