from django.db import models
from config.models import BaseModel

class UserNote(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="user_notes")
    sense = models.ForeignKey('ai.AISense', on_delete=models.CASCADE, related_name="notes")
    
    content = models.TextField() # Nội dung ghi chú của user
    version = models.IntegerField(default=0)

    class Meta:
        db_table = "user_note"
        unique_together = ("user", "sense") # Mỗi user có tối đa 1 ghi chú cho 1 nghĩa từ cố định
        indexes = [
            models.Index(fields=['user', 'sense']),
        ]

    def __str__(self):
        return f"Note({self.user_id} -> {self.sense_id})"