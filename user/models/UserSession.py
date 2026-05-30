from django.db import models
from config.models import BaseModel

class UserSession(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='user_sessions')
    
    # Liên kết với bảng Device chúng ta vừa tối ưu ở bước trước
    device = models.ForeignKey('user.Device', on_delete=models.CASCADE, related_name='sessions')
    
    # Thông tin mạng tại thời điểm kích hoạt phiên
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    
    # 🛠️ TINH CHỈNH: Không nên lưu cả cụm user_agent quá dài vào text field thô, 
    # chỉ cần lưu tên trình duyệt/thiết bị ngắn gọn được bóc tách từ middleware (ví dụ: "Safari - iOS")
    client_name = models.CharField(max_length=100, blank=True) 

    # 🛠️ BỔ SUNG CORE SECURITY: Lưu JTI (JWT ID) của Refresh Token tương ứng với phiên này
    # Khi user bấm "Đăng xuất từ xa", ta tìm đúng JTI này để cho vào Blacklist, Token đó lập tức vô hiệu hóa
    refresh_token_jti = models.CharField(max_length=255, unique=True, db_index=True, null=True, blank=True)

    last_active_at = models.DateTimeField(auto_now=True) # Cập nhật mỗi lần user tương tác online
    is_active = models.BooleanField(default=True)        # False nghĩa là phiên này đã bị revoke/đăng xuất

    class Meta:
        db_table = 'user_session'
        ordering = ['-last_active_at']
        indexes = [
            models.Index(fields=['user', 'is_active']),
        ]

    def __str__(self):
        return f"Session {self.id} for {self.user.email} ({self.client_name})"