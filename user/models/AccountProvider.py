from django.db import models
from config.models import BaseModel

class AccountProvider(BaseModel):
    PROVIDER_CHOICES = [
        ('google', 'Google'),
        ('facebook', 'Facebook'),
        ('apple', 'Apple'),
        ('github', 'GitHub'),
        ('x', 'X (Twitter)'),
        ('email', 'Email'),  # provider_user_id = email viết thường, mật khẩu nằm ở auth.User
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='providers')
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES)
    
    # 🛠️ BỔ SUNG BẮT BUỘC: ID của user bên phía nhà cung cấp (Ví dụ: ID chuỗi số của Google/Facebook)
    # Trường này dùng để map khi user bấm "Login with Google" ở những lần sau.
    provider_user_id = models.CharField(max_length=255, db_index=True)
    
    # URL profile hoặc access token từ bên thứ 3 (Thường dùng để sync lại avatar/friendlist nếu cần)
    profile_url = models.URLField(blank=True, null=True) 
    access_token = models.TextField(blank=True, null=True) # Apple: refresh token để thu hồi khi xoá tài khoản

    class Meta:
        db_table = 'account_provider'
        # 1 tài khoản Google / Apple / email chỉ gắn với 1 user
        constraints = [models.UniqueConstraint(fields=['provider', 'provider_user_id'], name='uniq_provider_identity')]
        # Một user không được liên kết 2 tài khoản Google khác nhau
        unique_together = ('user', 'provider') 
        indexes = [
            # Tối ưu cho luồng xử lý callback Đăng nhập: Tìm xem provider_user_id này thuộc về User nào hệ thống
            models.Index(fields=['provider', 'provider_user_id']),
        ]

    def __str__(self):
        return f"{self.get_provider_display()} -> {self.user_id}"