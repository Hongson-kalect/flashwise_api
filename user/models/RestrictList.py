
from config.models import BaseModel
from django.db import models

class RestrictList(BaseModel):
    """
    Người dùng chặn hoặc hạn chế người khác.
    Dùng để ẩn nội dung, bình luận hoặc tương tác.
    """
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='restricting_user')
    target = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='restricted_by')
    reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_muted = models.BooleanField(default=False)  # ẩn thông báo, nhưng không chặn
    is_blocked = models.BooleanField(default=True)  # chặn hoàn toàn
    metadata = models.JSONField(null=True, blank=True) # chặn toàn bộ hay chặn những giif

    class Meta:
        db_table = "restricted_list"
        unique_together = ('user', 'target')
        verbose_name = "Restricted User"
        verbose_name_plural = "Restricted Users"

    def __str__(self):
        return f"{self.user} - {self.target}"