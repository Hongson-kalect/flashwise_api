
from ai.models.AISense import AISense
from config.models import BaseModel
from django.db import models

class Note(BaseModel):
    sense = models.ForeignKey(AISense, on_delete=models.CASCADE, related_name='notes')
    content = models.TextField(null=True, blank=True)
    detail = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "note"
        ordering = ["-created_at"]
        constraints = [
            # CHÍ MẠNG: Đảm bảo 1 user chỉ có duy nhất 1 bản ghi note cho 1 nghĩa từ vựng cụ thể
            models.UniqueConstraint(
                fields=['sense', 'created_by'],
                condition=models.Q(is_deleted=False),
                name='unique_user_sense_note'
            )
        ]
        indexes = [
            # Tối ưu hóa truy vấn khi user muốn xem lại toàn bộ các note mình đã viết
            models.Index(fields=["created_by", "is_deleted"]),
            # Tối ưu hóa cho API lật thẻ bốc nhanh note của user hiện tại cho sense này
            models.Index(fields=["sense", "created_by"]),
        ]

    def __str__(self):
        # Trỏ vào word_value của sense để tránh crash code
        word_str = self.sense.word_value if self.sense else "Unknown"
        return f"Note by User {self.created_by_id} for {word_str}"
