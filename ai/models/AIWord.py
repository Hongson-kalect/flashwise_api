from django.contrib.postgres.indexes import GinIndex
from django.db import models
from config.models import BaseModel

class AIWord(BaseModel):
    # Bỏ db_index=True ở đây vì đã có trong phần indexes bên dưới
    value = models.CharField(max_length=255) 
    language_code = models.CharField(max_length=10, default="en") # Nên có default rõ ràng
    # Luồng chuẩn: PENDING -> PROCESSING -> COMPLETED, lỗi thì FAILED, từ không hợp lệ thì REJECTED, admin chặn thì BANNED
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('REJECTED', 'Rejected'),
        ('BANNED', 'Banned'), # Admin chặn (interact/moderation.py): get-word trả 404, không tạo lại
    ]
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    description = models.TextField(null=True, blank=True)
    # Phát âm chung của từ: AI chỉ sinh ở cấp từ. Nghĩa có ipas / audio riêng thì dùng của nghĩa (sense > word)
    ipas = models.JSONField(default=list, blank=True) # [{label, value, roman}]
    audio = models.CharField(max_length=1000, null=True, blank=True) # Đường dẫn file âm thanh; rỗng thì app đọc bằng giọng máy

    class Meta:
        db_table = "ai_word"
        ordering = ["value"]
        
        indexes = [
            # Tạo Index tổ hợp vì Sơn sẽ luôn tìm kiếm từ theo Ngôn ngữ nhất định
            models.Index(fields=["language_code", "value"], name="idx_lang_value"),
            # Index phục vụ cho Worker quét các từ đang chờ xử lý theo thời gian tạo
            models.Index(fields=["status", "created_at"], name="idx_status_created"),
            # Gợi ý từ gần giống khi gõ (ai-words/search, pg_trgm)
            GinIndex(fields=["value"], name="ai_word_value_trgm", opclasses=["gin_trgm_ops"]),
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