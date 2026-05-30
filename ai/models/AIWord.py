from django.db import models
from config.models import BaseModel

class AIWord(BaseModel):
    # Bỏ db_index=True ở đây vì đã có trong phần indexes bên dưới
    value = models.CharField(max_length=255) 
    language_code = models.CharField(max_length=10, default="en") # Nên có default rõ ràng
    status = models.CharField(max_length=50, default='PENDING') # Luồng chuẩn: PENDING -> PROCESSING -> COMPLETED
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "ai_word"
        ordering = ["value"]
        
        indexes = [
            # Tạo Index tổ hợp vì Sơn sẽ luôn tìm kiếm từ theo Ngôn ngữ nhất định
            models.Index(fields=["language_code", "value"], name="idx_lang_value"),
            # Index phục vụ cho Worker quét các từ đang chờ xử lý theo thời gian tạo
            models.Index(fields=["status", "created_at"], name="idx_status_created"),
        ]

        constraints = [
            # Khóa chặn: Đảm bảo không bao giờ bị trùng lặp cặp Từ + Ngôn ngữ đang hoạt động trong hệ thống
            models.UniqueConstraint(
                fields=['value', 'language_code'],
                condition=models.Q(is_active=True),
                name='unique_active_word_lang'
            )
        ]

    def __str__(self):
        return f"[{self.language_code.upper()}] {self.value} ({self.status})"