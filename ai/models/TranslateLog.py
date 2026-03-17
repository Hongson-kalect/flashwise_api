
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AIWord import AIWord
from config.models import BaseModel
from django.db import models

class TranslateLog(BaseModel):
    word = models.ForeignKey(AIWord, on_delete=models.SET_NULL, null=True,related_name='translate_logs')
    language_code = models.CharField(max_length=10, blank=True, null=True)
    type = models.CharField(max_length=50, blank=True, null=True)
    status = models.CharField(max_length=50, default='PROCESSING')

    class Meta:
        db_table = "translate_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["word"]),
            models.Index(fields=["language_code"]),
        ]

        constraints = [
            models.UniqueConstraint(
                fields=['word', 'language_code'],
                condition=models.Q(status='PROCESSING'),
                name='unique_translate_query'
            )
        ]

    def __str__(self):
        return f"Sense ({self.word}, {self.language_code})"