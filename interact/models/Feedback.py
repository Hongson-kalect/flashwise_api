from django.db import models
from config.models import BaseModel

# 1️⃣ Feedback
class Feedback(BaseModel):
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

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="feedbacks")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="bug")
    result = models.TextField(blank=True, null=True)
    accepted = models.BooleanField(default=False)
    notification = models.ForeignKey('user.Notification', on_delete=models.SET_NULL, blank=True, null=True) # Có options thông báo trả lời người dùng khi xử lý nó
    message = models.TextField()
    target_id = models.UUIDField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    class Meta:
        db_table = "feedback"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Feedback({self.type}, {self.user_id})"