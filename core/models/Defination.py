# models_language_level_snapshot.py
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

from config.models.BaseModel import BaseModel

class Defination(BaseModel):
    sub_id = models.CharField(max_length=100, blank=True, null=True, db_index=True) # uuid v7
    language_code = models.CharField(max_length=10, blank=True, null=True)
    value = models.JSONField(default=list, blank=True, null=True)
    bold = models.CharField(max_length=100, blank=True, null=True) #json index[][]..
    word = models.ForeignKey('core.Word', on_delete=models.SET_NULL, blank=True, null=True, related_name='definations')
    # translate = ArrayField(base_field=models.CharField(max_length=50, blank=True), blank=True,null=True)  # noun, verb, adj, ...
    score = models.IntegerField(default=100,null=True)
    roman = models.TextField(blank=True, null=True)
    ruby = models.TextField(blank=True, null=True) #json string[][]
    is_active = models.BooleanField(default=True, null=True)

    class Meta:
        db_table = "word defination"
        ordering = ["language_code"]
        indexes = [
            models.Index(
                fields=['is_active', 'is_deleted', 'language_code', '-created_at'],
                name='defi_active_lang_created_idx'
            ),
            models.Index(
                fields=['language_code','value', ],
                name='defi_word_lang_idx'
            ),
        ]

    def __str__(self):
        return f"{self.value}"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)
