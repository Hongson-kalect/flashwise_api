from django.db import models
from config.models import BaseModel


class AISenseMetadata(BaseModel):
    level = models.CharField(max_length=20, null=True, blank=True)
    synonyms = models.JSONField(default=list, blank=True)
    antonyms = models.JSONField(default=list, blank=True)
    relateds = models.JSONField(default=list, blank=True)
    forms = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)

    voted = models.IntegerField(default=0)
    devoted = models.IntegerField(default=0)
    likes = models.IntegerField(default=0)
    views = models.IntegerField(default=0)

    pos = models.CharField(max_length=50, null=True, blank=True) # Part of Speech
    ipas = models.JSONField(default=list) # {label, audio, value, reading, roman, ruby}
    
    is_valid = models.BooleanField(default=False)
    is_offensive = models.BooleanField(default=False)
    register = models.CharField(max_length=50, default='informal')
    image_desc = models.TextField(null=True, blank=True)
    image_link = models.URLField(max_length=500, null=True, blank=True)
    image_metadata = models.JSONField(default=dict, null=True, blank=True)  # {width, height, size, format, color_mode, etc.}

    class Meta:
        db_table = "sense_metadata"
        indexes = [
            models.Index(fields=["likes"]),
            models.Index(fields=["views"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"query: {self.target_type} by {getattr(self.user, 'email', str(self.user))}"
