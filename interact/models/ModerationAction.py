from django.db import models
from config.models import BaseModel


class ModerationAction(BaseModel):
    """
    Lịch sử ban / gỡ ban của admin (interact/moderation.py). Người làm = created_by.
    affected: trạng thái trước khi ban để gỡ ban khôi phục đúng (status cũ của từ, id bộ / nghĩa bị ẩn theo user...).
    """
    TARGET_CHOICES = [
        ("user", "User"), # target_id = UserProfile.id
        ("collection", "Collection"),
        ("word", "Word"),
        ("sense", "Sense"),
    ]
    ACTION_CHOICES = [
        ("ban", "Ban"),
        ("unban", "Unban"),
    ]

    target_type = models.CharField(max_length=20, choices=TARGET_CHOICES)
    target_id = models.UUIDField()
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    reason = models.TextField(blank=True, default='')
    expires_at = models.DateTimeField(null=True, blank=True) # Chỉ ban user: hết hạn tự mở, null = vĩnh viễn
    affected = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "moderation_action"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["target_type", "target_id", "-created_at"])]

    def __str__(self):
        return f"{self.action} {self.target_type}:{self.target_id}"
