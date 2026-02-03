from django.db import models
from config.models import BaseModel
from django.contrib.postgres.fields import ArrayField

class Word(BaseModel):
    sub_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    value = models.CharField(max_length=255)
    reading = models.TextField(blank=True, null=True) # cách hiển thị full hiragana
    roman = models.TextField(blank=True, null=True) # cách đọc romaji
    language_code = models.CharField(max_length=10, blank=True, null=True)
    synonyms = models.JSONField(default=list, blank=True, null=True) 
    antonyms = models.JSONField(default=list, blank=True, null=True)
    relateds = models.JSONField(default=list, blank=True, null=True)
    word_info = models.ForeignKey('core.WordInfo', on_delete=models.SET_NULL, null=True, blank=True, related_name='words')
    note = models.TextField(blank=True, null=True)
    rubys = models.JSONField(blank=True, null=True) # Khóa liên kết đến kanji
    score = models.IntegerField(default=100, null=True)
    is_active = models.BooleanField(default=True, null=True)
    is_fixed = models.BooleanField(default=False, null=True)

    class Meta:
        db_table = 'word'
        ordering = ['value']
        indexes = [
        models.Index(fields=['value']),  # tìm kiếm theo value
        models.Index(fields=['language_code', 'value', '-created_at']),  # filter theo language_code + value
        models.Index(fields=['-created_at']),  # tìm kiếm theo value
]

    def __str__(self):
        return f"{self.value} ({self.language_code or ''})"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)
