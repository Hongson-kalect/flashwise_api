from config.models import BaseModel
from django.db import models
from django.utils import timezone


class SnapShot(BaseModel):
    """
    Snapshot: lưu lại ảnh chụp nhanh của một entity ở một thời điểm nhất định.
    Thường dùng cho backup, log hoặc debug UI.
    snap: dữ liệu JSON snapshot.
    """
    type = models.CharField(max_length=50)  # ví dụ: 'word', 'collection', 'quiz'
    target_id = models.UUIDField(null=True, blank=True)
    snap = models.JSONField(default=dict, blank=True)
    
    class Meta:
        db_table = "snap_short"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["type"]),
            models.Index(fields=["target_id"]),
        ]

    def __str__(self):
        return f"SnapShort({self.type} - {self.target_id})"