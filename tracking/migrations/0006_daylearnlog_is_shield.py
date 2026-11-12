from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0005_reviewlog_attempts_reviewlog_hint_count_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='daylearnlog',
            name='is_shield',
            field=models.BooleanField(default=False),
        ),
    ]
