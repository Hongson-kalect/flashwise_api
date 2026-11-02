# 4️⃣ Report
from config.models import BaseModel
from django.db import models


class Report(BaseModel):
    TARGET_CHOICES = [
        ("word", "Word"),
        ("sense", "Sense"),
        ("collection", "Collection"),
        ("quiz", "Quiz"),
        ("user", "User"), # target_id = UserProfile.id
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
    # Kết quả xử lý (interact/views.py resolve_reports), quyết định cộng / trừ uy tín người report
    OUTCOME_CHOICES = [
        ("accepted", "Accepted"),   # Report đúng
        ("duplicate", "Duplicate"), # Đúng nhưng đã có người báo / đã sửa: không cộng không trừ
        ("rejected", "Rejected"),   # Không đúng
        ("spam", "Spam"),           # Báo bừa / quấy rối
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
    weight = models.FloatField(default=0.5) # Độ tin cậy của người report lúc gửi (user/reputation.py), cộng dồn để xếp hàng duyệt
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, blank=True, null=True)
    resolved_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name="resolved_reports")
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "report"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "target_type", "target_id"])]

    def __str__(self):
        return f"Report({self.target_type}:{self.target_id})"