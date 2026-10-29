from django.db import models
from config.models import BaseModel

class UserNote(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="user_notes")
    sense = models.ForeignKey('ai.AISense', on_delete=models.CASCADE, related_name="user_notes")

    content = models.TextField() # Nội dung ghi chú của user
    version = models.IntegerField(default=0)

    class Meta:
        db_table = "user_note"
        constraints = [
            # Mỗi user có tối đa 1 ghi chú (chưa xoá) cho 1 nghĩa từ cố định
            models.UniqueConstraint(
                fields=["user", "sense"],
                condition=models.Q(is_deleted=False),
                name="unique_user_sense_note",
            )
        ]
        indexes = [
            models.Index(fields=['user', 'updated_at'], name='idx_note_sync'),
        ]

    def __str__(self):
        return f"Note({self.user_id} -> {self.sense_id})"
