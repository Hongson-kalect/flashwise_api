# models_language_level_snapshot.py
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

from config.models.BaseModel import BaseModel

class Example(BaseModel):
    sub_id = models.CharField(max_length=100, blank=True, null=True, db_index=True) # uuid v7
    value = models.TextField(null=True)
    word = models.ForeignKey('core.Word', on_delete=models.SET_NULL, blank=True, null=True, related_name='word_examples') # word id - Cân nhắc dịch chuyển sang từ của người dùng thay vì official
    defination = models.ForeignKey('core.Defination', on_delete=models.SET_NULL, blank=True, null=True, related_name='defination_examples') # word id - Cân nhắc dịch chuyển sang từ của người dùng thay vì official
    bold = models.JSONField(default=list, blank=True, null=True) #json index[][]..
    language_code = models.CharField(max_length=10, blank=True, null=True)
    score = models.IntegerField(default=100, null=True)
    roman = models.TextField(blank=True, null=True)
    bold_roman = models.JSONField(default=list, blank=True, null=True) #json index[][]..
    ruby = models.JSONField(default=list, blank=True, null=True) #json string[][]
    is_active = models.BooleanField(default=True, null=True)

    class Meta:
        db_table = "word example"
        ordering = ["value"]
        indexes=[
            models.Index(fields=['is_active','is_deleted','language_code','score']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.value}"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)
