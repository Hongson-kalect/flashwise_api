

from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AIWord import AIWord
from config.models import BaseModel
from django.db import models

class AISense(BaseModel):
    word = models.ForeignKey(AIWord, related_name='senses', on_delete=models.CASCADE)
    word_value = models.TextField(null=True, blank=True) # Hiện luôn value để đỡ phải join
    original = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='original_sense') # Bản gốc, dùng để compare hiển thị cho người dùng (unique original - Không có thì giời biết 2 bản có phải cùng 1 sense không)
    previous = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='previous_sense') # Bản trước đó, dùng để lấy dữ liệu frozen
    origins = models.JSONField(default=list) # [id1, id2...]
    metadata = models.ForeignKey(AISenseMetadata, on_delete=models.SET_NULL, null=True, related_name='senses')
    preview =models.JSONField(default=dict)
    image_preview = models.URLField(max_length=500, null=True, blank=True)
    image_context = models.ForeignKey('core.ImageContext', on_delete=models.SET_NULL, null=True, blank=True)

    language_code = models.CharField(max_length=10, blank=True, null=True)

    contents = models.JSONField(default=dict, null=True, blank=True)
    delta = models.JSONField(default=dict, null=True, blank=True)
    news = models.JSONField(default=list) # những content trong news sẽ có thể update tùy ý

    is_offensive = models.BooleanField(default=False, null=True)
    is_frozen = models.BooleanField(default=True, null=True)
    # True = can't update, always create new. False = can update news content. None = can update all
    # When update content not in news with is_frozen = False, create new content, replace in contents and update news 

    versions = models.IntegerField(default=1)
    is_official = models.BooleanField(default=True)
    is_ai_created = models.BooleanField(default=True)

    class Meta:
        db_table = "ai_sense"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["word"]),
            models.Index(fields=["language_code"]),
            models.Index(fields=["metadata"]),
            models.Index(fields=["original"]),
            models.Index(fields=["previous"]),
            models.Index(fields=["is_frozen"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["is_deleted"]),
        ]

    def __str__(self):
        return f"Sense ({self.word_value}, {self.metadata.pos})"