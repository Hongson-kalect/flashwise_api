from django.db import models
from config.models import BaseModel

class AdminLog(BaseModel):
    ACTION_CHOICES = [
        ("ban", "Ban"),
        ("unban", "Unban"),
        ("delete", "Delete"),
        ("restore", "Restore"),
        ("update", "Update"),
        ("note", "Note"),
    ]

    admin = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name="admin_logs")
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    target_id = models.UUIDField(null=True, blank=True)
    target_type = models.CharField(max_length=100, blank=True)
    reason = models.TextField(blank=True)
    meta = models.JSONField(default=dict, blank=True)  # optional extra info
    is_success = models.BooleanField(default=False)

    class Meta:
        db_table = "admin_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["admin", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
            # Index phức hợp phục vụ việc tìm nhanh lịch sử xử lý của 1 thực thể cụ thể
            models.Index(fields=["target_type", "target_id"]), 
        ]

    def __str__(self):
        admin_repr = getattr(self.admin, "email", str(self.admin)) if self.admin else "system"
        return f"AdminLog({self.action}) on {self.target_type or 'n/a'} by {admin_repr}"
