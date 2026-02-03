# 4️⃣ Report
from config.models import BaseModel
from django.db import models


class Report(BaseModel):
    TARGET_CHOICES = [
        ("word", "Word"),
        ("collection", "Collection"),
        ("quiz", "Quiz"),
    ]
    TYPE_CHOICES = [
            ("bug", "Bug"),
            ("content", "Content"),
            ("ui", "UI/UX"),
            ("feature", "Feature Request"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("reviewed", "Reviewed"),
        ("resolved", "Resolved"),
        ("ignored", "Ignored"),
    ]


    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="reports")
    target_id = models.UUIDField()
    target_type = models.CharField(max_length=20, choices=TARGET_CHOICES)

    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="bug")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    reason = models.CharField(max_length=255)
    message = models.TextField(blank=True, null=True)
    result = models.TextField(blank=True, null=True)  # phản hồi xử lý của admin
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "report"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Report({self.target_type}:{self.target_id})"