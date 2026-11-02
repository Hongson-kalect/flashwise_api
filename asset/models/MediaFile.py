from django.db import models

from config.models import BaseModel


class MediaFile(BaseModel):
    """
    File user tải lên (ảnh bộ từ / nghĩa, audio phát âm), lưu qua core/storages.py.
    `key` là khoá tương đối, app ghép với media_base_url; người tải = created_by.
    """
    KIND_CHOICES = [('image', 'Image'), ('audio', 'Audio')]

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    key = models.CharField(max_length=500, unique=True)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField(default=0)  # byte, sau khi nén (ảnh)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = 'media_file'
        indexes = [models.Index(fields=['created_by', '-created_at'])]

    def __str__(self):
        return f'{self.kind}: {self.key}'
