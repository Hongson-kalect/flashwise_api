from django.db import models
from config.models import BaseModel

class AIWord(BaseModel):
    value = models.CharField(max_length=255, db_index=True)
    language_code = models.CharField(max_length=10, blank=True, null=True)
    status = models.CharField(max_length=50, default='PROCESSING') # PENDING, PROCESSING, COMPLETED, FAILED
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "ai_word"
        ordering = ["value"]
        indexes = [
            models.Index(fields=["language_code"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["value"]),
        ]

        constraints = [
            models.UniqueConstraint(
                fields=['value', 'language_code'],
                condition=models.Q(is_active=True, status='PROCESSING'),
                name='unique_word'
            )
        ]

    def __str__(self):
        return self.value
   