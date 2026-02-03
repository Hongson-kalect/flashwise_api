# models_theme_images.py
from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.utils import timezone

from config.models import BaseModel

User = get_user_model()


class Theme(BaseModel):
    """
    Theme: lưu cấu hình màu, font, và các metadata cho giao diện.
    color_palette: JSON structure, ví dụ:
      {
        "primary": "#1E88E5",
        "on_primary": "#FFFFFF",
        "background": "#F6F8FB",
        "surface": "#FFFFFF",
        "accent": "#FFB300",
        "text_primary": "#111827",
        "text_secondary": "#6B7280"
      }
    """
    name = models.CharField(max_length=150, unique=True)
    color_palette = models.JSONField(default=dict, blank=True)
    font = models.CharField(max_length=150, blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        db_table = "theme"
        ordering = ["-is_default", "name"]
        indexes = [
            models.Index(fields=["is_default"]),
        ]

    def __str__(self):
        return f"Theme({self.name})"

