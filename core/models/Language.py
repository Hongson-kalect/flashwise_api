# models_language_level_snapshot.py
from django.db import models
from django.utils import timezone

from config.models.BaseModel import BaseModel

class Language(models.Model):
    """
    Bảng lưu danh sách ngôn ngữ.
    Dùng code ISO (en, vi, zh, ja, ...).
    """
    DIRECTION_CHOICES = [
        ("ltr", "Left to Right"),
        ("rtl", "Right to Left"),
    ]

    code = models.CharField(max_length=10, unique=True, primary_key=True)
    name = models.CharField(max_length=100)
    native_name = models.CharField(max_length=100, blank=True, null=True)
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES, default="ltr",null=True)
    flag_emoji = models.CharField(max_length=10, blank=True, null=True)

    is_supported = models.BooleanField(default=True, null=True)
    is_deleted = models.BooleanField(default=False, null=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='%(class)s_created')
    updated_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='%(class)s_updated')
    is_active = models.BooleanField(default=True, null=True)

    class Meta:
        db_table = "language"
        ordering = ["code"]
        indexes=[
            models.Index(fields=['code']),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)
