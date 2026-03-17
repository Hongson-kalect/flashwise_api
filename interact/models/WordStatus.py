from django.db import models
from config.models import BaseModel


# 1️⃣ WordStatus
class WordStatus(BaseModel):
    STATUS_CHOICES = [
        ("learning", "Learning"),
        ("learned", "Learned"),
        ("reviewing", "Reviewing"),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="word_statuses")
    word_sub_id = models.CharField(max_length=50)  # lưu subId để track dù Word thay đổi version

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="learning")
    level = models.PositiveIntegerField(default=0)
    is_mastered = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)
    is_avoid = models.BooleanField(default=False)
    reason = models.TextField(blank=True, null=True)

    last_seen_at = models.DateTimeField(blank=True, null=True)
    last_learn_at = models.DateTimeField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"WordStatus({self.word_sub_id}, {self.status})"