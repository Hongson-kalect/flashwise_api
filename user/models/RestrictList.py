from django.db import models
from django.db.models import UniqueConstraint
from config.models import BaseModel

class RestrictList(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='restricting_user')
    target = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='restricted_by')
    
    reason = models.TextField(blank=True, null=True)
    is_muted = models.BooleanField(default=False)  # Ẩn bài viết/bình luận/thông báo của đối phương, nhưng họ không biết
    is_blocked = models.BooleanField(default=True) # Chặn hoàn toàn tương tác (Không cho xem profile, không thấy nhau trên BXH)
    metadata = models.JSONField(null=True, blank=True) 

    class Meta:
        db_table = "restricted_list"
        verbose_name = "Restricted User"
        verbose_name_plural = "Restricted Users"
        
        constraints = [
            # 🛠️ CHUẨN HÓA: Thay thế unique_together bằng UniqueConstraint hiện đại hơn
            UniqueConstraint(fields=['user', 'target'], name='unique_user_target_restrict')
        ]
        
        indexes = [
            # 🛠️ TỐI ƯU HIỆU NĂNG: Index quan trọng nhất để Server quét nhanh khi load luồng dữ liệu công cộng
            # Ví dụ: "Lấy tất cả ID mà user này đã chặn để lọc bỏ khỏi danh sách bình luận"
            models.Index(fields=['user', 'is_blocked', 'is_muted']),
        ]

    def __str__(self):
        return f"{self.user.email} restricted {self.target.email} (Blocked: {self.is_blocked})"