"""verify / ai_verify từ boolean thành JSON (danh sách mã ngôn ngữ; ['*'] = cả dòng, không theo ngôn ngữ)."""
from django.db import migrations, models

MODELS = ['aisense', 'aisensemetadata', 'concept']


def _target_langs(contents, source):
    c = contents or {}
    found = set((c.get('definition') or {}).keys()) | set((c.get('translations') or {}).keys())
    return sorted(found - {source})


def forward(apps, schema_editor):
    for name in MODELS:
        Model = apps.get_model('ai', name)
        for row in Model.objects.filter(models.Q(verify_old=True) | models.Q(ai_verify_old=True)).iterator():
            if name == 'aisense':
                langs = _target_langs(row.contents, row.language_code)
                if row.verify_old:
                    row.verify = sorted(set(langs) | {row.language_code})
                if row.ai_verify_old:
                    state = row.verify_state or {}
                    row.ai_verify = [l for l in langs if (state.get(l) or {}).get('state') == 'ok'] or langs
            else:
                row.verify = ['*'] if row.verify_old else []
                row.ai_verify = ['*'] if row.ai_verify_old else []
            row.save(update_fields=['verify', 'ai_verify'])


def backward(apps, schema_editor):
    for name in MODELS:
        Model = apps.get_model('ai', name)
        for row in Model.objects.all().iterator():
            row.verify_old, row.ai_verify_old = bool(row.verify), bool(row.ai_verify)
            row.save(update_fields=['verify_old', 'ai_verify_old'])


class Migration(migrations.Migration):
    dependencies = [('ai', '0013_verify_columns')]

    operations = (
        [migrations.RenameField(m, f, f + '_old') for m in MODELS for f in ('verify', 'ai_verify')]
        + [migrations.AddField(m, f, models.JSONField(blank=True, default=list)) for m in MODELS for f in ('verify', 'ai_verify')]
        + [migrations.RunPython(forward, backward)]
        + [migrations.RemoveField(m, f + '_old') for m in MODELS for f in ('verify', 'ai_verify')]
    )
