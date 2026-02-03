from pyexpat import model
from django.contrib.postgres.fields import ArrayField
from re import sub
from django.db import models
from config.models import BaseModel
from django.contrib.postgres.indexes import GinIndex

class WordInfo(BaseModel):
    # word = models.ForeignKey(Word, on_delete=models.CASCADE, related_name='infos')
    # word_sub_id = models.CharField(max_length=100, blank=True, null=True)
    sub_id = models.CharField(max_length=100, blank=True, null=True)
    pos = models.CharField(max_length=100, blank=True, null=True)  # part of speech
    ipas = models.JSONField(default=list, blank=True, null=True) 
    audios = models.JSONField(default=list, blank=True, null=True)  # list of audio URLs
    images = models.JSONField(default=list, blank=True, null=True) 
    usage = models.TextField(blank=True, null=True)
    etymology = models.TextField(blank=True, null=True)
    interesting_info = models.TextField(blank=True, null=True)  # ví dụ: "fun facts", "regional usage"
    tip = models.TextField(blank=True, null=True)
    tags = models.JSONField(default=list, blank=True, null=True)
    topics = models.JSONField(default=list, blank=True, null=True)
    level = models.CharField(max_length=50, blank=True, null=True) # a1-c2, n1-n5, ...

    class Meta:
        db_table = 'word_info'
        ordering = ['-created_at']
        indexes=[
            models.Index(fields=['-created_at']),
            models.Index(fields=['pos']),
            GinIndex(fields=['tags'], name='wordinfo_tags_gin', opclasses=['jsonb_path_ops']),
        ]

    def __str__(self):
        return f"WordInfo for {self.id}"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)
