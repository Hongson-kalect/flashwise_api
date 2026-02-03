from config.models import BaseModel
from django.db import models

class Topic(BaseModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    languageCode = models.CharField(max_length=10, blank=True, null=True)
    isSystemTag = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_global = models.BooleanField(default=True)

    def __str__(self):
        return self.name