from django.db import models
from config.models import BaseModel

class Notification(BaseModel):
    TYPE_CHOICES = [
        ('system', 'System'),     # Thông báo toàn hệ thống, bảo trì, sự kiện
        ('progress', 'Progress'), # Thông báo liên quan tới Gamification, BXH, Achievement
        ('remind', 'Remind'),     # Thông báo nhắc nhở từ Server (nếu có chiến dịch đặc biệt)
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='notifications')
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    title = models.CharField(max_length=255)
    content = models.TextField(blank=True)
    is_read = models.BooleanField(default=False, db_index=True) # Đánh index để lọc nhanh các thông báo chưa đọc

    # 🛠️ BỔ SUNG: Dữ liệu điều hướng (Deep Linking)
    # Ví dụ: {"screen": "CollectionDetail", "params": {"id": "uuid-v7-bo-tu"}}
    # Khi user chạm vào thông báo trên điện thoại, app sẽ bóc cục json này để mở đúng màn hình
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'notification'
        ordering = ['-created_at']
        indexes = [
            # Tối ưu cho API lấy danh sách thông báo mới nhất của User:
            # Notification.objects.filter(user=user).order_by('-created_at')
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f"Notification({self.type}) to {self.user.email} | Read: {self.is_read}"