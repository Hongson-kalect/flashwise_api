from django.db import models
from config.models import BaseModel


class ReputationEvent(BaseModel):
    """
    Nhật ký uy tín (ẩn với user): mỗi lần được cộng / trừ là 1 dòng. Điểm trên UserProfile chỉ là kết quả tính lại
    từ bảng này theo công thức hiện tại (user/reputation.py), đổi công thức thì tính lại được cho mọi user.
    """
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='reputation_events')
    kind = models.CharField(max_length=30) # user/reputation.py POINTS
    points = models.IntegerField(default=0) # Chỉ dùng cho kind=manual (admin cộng / trừ tay)
    target_type = models.CharField(max_length=20, blank=True, null=True)
    target_id = models.UUIDField(blank=True, null=True)
    report = models.ForeignKey('interact.Report', on_delete=models.SET_NULL, null=True, blank=True, related_name='reputation_events')
    note = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = "reputation_event"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "kind"])]

    def __str__(self):
        return f"{self.user_id} {self.kind}"
