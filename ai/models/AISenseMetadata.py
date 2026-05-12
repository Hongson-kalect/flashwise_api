from django.db import models
from config.models import BaseModel
from django.contrib.postgres.fields import ArrayField


class AISenseMetadata(BaseModel):
    advanced = models.JSONField(default=dict, null=True, blank=True) # collocation, idiom, synonyms, antonyms, relateds, forms, etc.

    tags = models.JSONField(default=list, blank=True)

    image_keywords = models.JSONField(null=True, blank=True, default=list)
    image_link = models.URLField(max_length=500, null=True, blank=True)
    image_metadata = models.JSONField(default=dict, null=True, blank=True)  # {width, height, size, format, color_mode, etc.}

    class Meta:
        db_table = "sense_metadata"
        indexes = [
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

