# models_logs.py
from django.db import models
from django.utils import timezone
from config.models import BaseModel
from django.contrib.postgres.fields import ArrayField

# Bảng này có thể sẽ không dùng đến, các bảng log gần như đã có thể thực hiện đầy đủ các chức năng cần thiết.

class LearnLog(BaseModel):
    """
    Lưu từng hành động học 1 từ (success/fail)
    """
    RESULT_CHOICES = [
        ("success", "Success"),
        ("fail", "Fail"),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="daily_logs")
    date = models.DateField(default=timezone.now)
    words_learned = ArrayField(default=list, base_field=models.UUIDField(), blank=True)
    words_relearned = ArrayField(default=list, base_field=models.UUIDField(), blank=True)

    learn_time = models.PositiveIntegerField(default=0, help_text="Thời gian học (phút hoặc giây)")

    xp_earned = models.PositiveIntegerField(default=0)

    progress = models.JSONField(default=dict, blank=True) # Mô tả lại cách user trả lời
    metadata = models.JSONField(default=dict, blank=True) # đúng sai dư lào, số thống kê

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="learn_logs")
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