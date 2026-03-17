# models_logs.py
from django.db import models
from django.utils import timezone
from config.models import BaseModel

# Bảng này có thể sẽ không dùng đến, các bảng log gần như đã có thể thực hiện đầy đủ các chức năng cần thiết.

class LearnLog(BaseModel):
    """
    Lưu từng hành động học 1 từ (success/fail)
    """
    RESULT_CHOICES = [
        ("success", "Success"),
        ("fail", "Fail"),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="learn_logs")
    session_id = models.UUIDField(null=True, blank=True)
    learned_at = models.DateTimeField(default=timezone.now)
    result = models.CharField(max_length=10, choices=RESULT_CHOICES)
    xp_earned = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "learn_log"
        ordering = ["-learned_at"]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["learned_at"]),
        ]

    def __str__(self):
        return f"LearnLog({self.user_id}, {self.result}, {self.learned_at})"