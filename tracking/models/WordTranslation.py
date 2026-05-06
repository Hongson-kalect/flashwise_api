from config.models import BaseModel
from django.db import models

class WordTranslation(models.Model):
    # ID có thể là UUIDv7 hoặc text value tùy kiến trúc từ điển của bạn
    source_word = models.CharField(max_length=255) # Ví dụ: 'bàn' (VI)
    target_word = models.CharField(max_length=255) # Ví dụ: 'table' (EN)
    language_code = models.CharField(max_length=10)
    translate_language_code = models.CharField(max_length=10)
    
    # Điểm số dịch thuật tích lũy từ AI ban đầu + hành động switch sense của user
    translate_score = models.IntegerField(default=100) # = 0 thì sẽ không hiển thị
    created_at = models.DateTimeField(auto_now_add=True) # Score / (Hours + 2)^1.8)

    class Meta:
        unique_together = ('source_word', 'target_word')
        indexes = [
            # Tối ưu cho luồng Reverse Search: Gõ chữ tiếng Việt -> Lấy các từ tiếng Anh có điểm cao nhất
            models.Index(fields=['source_word', '-translate_score']),
        ]