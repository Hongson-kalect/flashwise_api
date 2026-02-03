
from ai.models.AISense import AISense
from config.models import BaseModel
from django.db import models

class Note(BaseModel):
    sense = models.ForeignKey(AISense, on_delete=models.CASCADE, related_name='notes')
    user = models.CharField(max_length=255) # Thường liên kết với User model
    content = models.TextField(null=True, blank=True)
    detail = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "note"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["sense"]),
        ]

    def __str__(self):
        return f"note {self.sense[:50]}"

