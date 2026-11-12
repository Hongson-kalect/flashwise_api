from django.contrib.postgres.operations import UnaccentExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Tìm không dấu (gợi ý từ theo nghĩa tiếng Việt: gõ "nha" ra "nhà")."""

    dependencies = [('ai', '0014_verify_json')]

    operations = [UnaccentExtension()]
