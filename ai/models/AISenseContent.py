
from enum import unique
from django.utils import timezone
from email.policy import default
from config.models import BaseModel
from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.contrib.auth import get_user_model

User = get_user_model()

class AISenseContent(BaseModel):
    # content_type = (
    #                 ('translate', 'Translate'),
    #                 ('definition', 'Definition'),
    #                 ('usage', 'Usage'),
    #                 ('example', 'Example'),
    #                 ('definition_translate', 'Definition Translate'),
    #                 ('usage_translate', 'Usage Translate'),
    #                 ('example_translate', 'Example Translate'),)
    # parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True)
    value = models.JSONField(null=True, blank=True)
    type = models.CharField(max_length=100, blank=True, null=True)
    audio = models.URLField(max_length=500, null=True, blank=True)
    reading = models.TextField(blank=True, null=True)
    roman = models.TextField(blank=True, null=True) # cách đọc romaji
    ruby = models.TextField(blank=True, null=True) # cách đọc romaji
    language_code = models.CharField(max_length=10, blank=True, null=True)
    is_ai_created = models.BooleanField(default=True)

    #  Những dữ liệu trên đều dễ lấy nếu query bảng LearnSession nhưng nếu làm dashboard thì sẽ phải xử lý 1 lúc => Cần các dữ liệu chính để dashboard

    class Meta:
        db_table = "ai_sense_content"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["language_code"]),
            models.Index(fields=["type"]),
        ]

    def __str__(self):
        return f"{self.type} ({self.value[:50]}...)"