from django.db import models
from config.models import BaseModel


class Asset(BaseModel):
    """
    Định lưu người nào ứng với dữ liệu nào. Nhưng có vẻ không cần nếu logic tốt, nếu dùng nó thì sẽ luôn mất mấy lần join
    """
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='user_asset')
    asset_type = models.CharField(max_length=20)
    asset_id = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "asset"
        indexes = [models.Index(fields=['user', 'is_active'])]
        verbose_name = "User Asset"
        verbose_name_plural = "Users Asset"

    def __str__(self):
        return f"{self.user} (banned by {self.banned_by or 'system'})"


