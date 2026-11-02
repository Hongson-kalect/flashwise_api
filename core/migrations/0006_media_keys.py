from django.db import migrations

OLD_PREFIX = '/upload/'


def to_keys(apps, schema_editor):
    """Ảnh cũ lưu '/upload/images/v1/a.webp' (đường dẫn theo server Django) -> khoá 'images/v1/a.webp' (core/storages.py)."""
    for app, model, field in (('core', 'ImageLibrary', 'url'), ('ai', 'AISense', 'image_preview')):
        Model = apps.get_model(app, model)
        rows = Model.objects.filter(**{f'{field}__startswith': OLD_PREFIX})
        for row in rows.only('pk', field):
            setattr(row, field, getattr(row, field)[len(OLD_PREFIX):])
            row.save(update_fields=[field])


def to_paths(apps, schema_editor):
    for app, model, field in (('core', 'ImageLibrary', 'url'), ('ai', 'AISense', 'image_preview')):
        Model = apps.get_model(app, model)
        for row in Model.objects.filter(**{f'{field}__startswith': 'images/'}).only('pk', field):
            setattr(row, field, OLD_PREFIX + getattr(row, field))
            row.save(update_fields=[field])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_relax_collection_item_unique'),
        ('ai', '0007_moderation'),
    ]

    operations = [
        migrations.RunPython(to_keys, to_paths),
    ]
