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
        unique_together = ('source_word', 'target_word', 'language_code', 'translate_language_code')
        
        indexes = [
            # INDEX CỐT LÕI 1: Dịch xuôi thông minh (Ví dụ: Tra từ 'bàn' hệ VI -> Lấy các từ EN có điểm cao nhất)
            models.Index(fields=['language_code', 'source_word', '-translate_score']),
            
            # INDEX CỐT LÕI 2: Hỗ trợ ngược lại (Ví dụ: Gõ từ 'table' hệ EN -> Gợi ý nhanh từ VI có điểm cao nhất)
            models.Index(fields=['translate_language_code', 'target_word', '-translate_score']),
        ]

    def __str__(self):
        return f"Translation({self.source_word} [{self.language_code}] → {self.target_word} | Score: {self.translate_score})"