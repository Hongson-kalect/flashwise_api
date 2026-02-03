from config.models import BaseModel
from django.db import models

class Tag(BaseModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    languageCode = models.CharField(max_length=10, blank=True, null=True)
    isSystemTag = models.BooleanField(default=False)

    def __str__(self):
        return self.name