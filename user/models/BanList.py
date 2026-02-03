from django.db import models
from config.models import BaseModel

class BanList(BaseModel):
    """
    Danh sách người dùng bị hệ thống chặn hoàn toàn.
    Có thể do vi phạm, spam, hoặc yêu cầu admin.
    """
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='banned_user')
    reason = models.TextField(blank=True, null=True)
    banned_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='banned_by_admin'
    )
    start_at = models.DateTimeField(auto_now_add=True)
    end_at = models.DateTimeField(blank=True, null=True)  # null = vĩnh viễn
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "banned_list"
        indexes = [models.Index(fields=['user', 'is_active'])]
        verbose_name = "Banned User"
        verbose_name_plural = "Banned Users"

    def __str__(self):
        return f"{self.user} (banned by {self.banned_by or 'system'})"


