# models_language_level_snapshot.py
from django.db import models
from django.utils import timezone
from django.contrib.postgres.fields import ArrayField

from config.models.BaseModel import BaseModel

class WordForm(BaseModel):
    sub_id = models.CharField(max_length=100, blank=True, null=True, db_index=True) # uuid v7
    value = models.TextField(max_length=50, blank=True, null=True)
    word = models.ForeignKey('core.Word', on_delete=models.SET_NULL, blank=True, null=True, related_name='word_forms')
    type = models.JSONField(default=list, blank=True, null=True) # noun, verb, adj, ...
    roman = models.TextField( blank=True, null=True)
    ruby = models.TextField( blank=True, null=True) #json string[][]

    class Meta:
        db_table = "word form"
        ordering = ["value","type"]

        indexes =[
            models.Index(fields=['value']),
        ]

    def __str__(self):
        return f"{self.value}"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)
