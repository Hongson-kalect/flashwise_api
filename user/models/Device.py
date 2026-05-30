from django.db import models
from config.models import BaseModel

class Device(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='devices')
    
    # 🛠️ CHUẨN HÓA: device_id nên là mã định danh duy nhất của phần cứng (UUID của thiết bị di động)
    device_id = models.CharField(max_length=255)
    
    os = models.CharField(max_length=50) # e.g., "ios", "android", "web"
    app_version = models.CharField(max_length=50) # Phục vụ debug logic sync khi có xung đột phiên bản app
    
    # 🛠️ BỔ SUNG: Token dùng để bắn Push Notification (FCM Token / APNS Token)
    push_token = models.TextField(blank=True, null=True) 
    
    # 🛠️ BỔ SUNG: Tên thiết bị do user đặt hoặc tự nhận diện (Ví dụ: "iPhone 15 Pro Max", "Samsung S24")
    device_name = models.CharField(max_length=150, blank=True, null=True)
    
    last_seen_at = models.DateTimeField(auto_now=True, null=True) # Tự động cập nhật mỗi khi user gọi API Sync

    class Meta:
        db_table = 'device'
        ordering = ['-last_seen_at']
        
        # 🛠️ RÀNG BUỘC CỐT LÕI: Mỗi cặp User + Device_ID chỉ tồn tại duy nhất 1 dòng bản ghi
        unique_together = ('user', 'device_id')
        
        indexes = [
            # Tối ưu cho luồng chạy Background Job quét Token để bắn thông báo nhắc học theo giờ
            models.Index(fields=['user', '-last_seen_at']),
        ]

    def __str__(self):
        return f"{self.device_name or self.os} ({self.user.email})"