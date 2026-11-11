"""ImageContext.verify / ai_verify từ boolean thành JSON (['*'] = đã kiểm)."""
from django.db import migrations, models


def forward(apps, schema_editor):
    Model = apps.get_model('core', 'ImageContext')
    for row in Model.objects.filter(models.Q(verify_old=True) | models.Q(ai_verify_old=True)).iterator():
        row.verify = ['*'] if row.verify_old else []
        row.ai_verify = ['*'] if row.ai_verify_old else []
        row.save(update_fields=['verify', 'ai_verify'])


def backward(apps, schema_editor):
    Model = apps.get_model('core', 'ImageContext')
    for row in Model.objects.all().iterator():
        row.verify_old, row.ai_verify_old = bool(row.verify), bool(row.ai_verify)
        row.save(update_fields=['verify_old', 'ai_verify_old'])


class Migration(migrations.Migration):
    dependencies = [('core', '0008_imagecontext_verify')]

    operations = [
        migrations.RenameField('imagecontext', 'verify', 'verify_old'),
        migrations.RenameField('imagecontext', 'ai_verify', 'ai_verify_old'),
        migrations.AddField('imagecontext', 'verify', models.JSONField(blank=True, default=list)),
        migrations.AddField('imagecontext', 'ai_verify', models.JSONField(blank=True, default=list)),
        migrations.RunPython(forward, backward),
        migrations.RemoveField('imagecontext', 'verify_old'),
        migrations.RemoveField('imagecontext', 'ai_verify_old'),
    ]
