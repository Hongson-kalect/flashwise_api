# có thể không dùng đến
from config.models import BaseModel
from django.db import models


class Version(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="entity_tags")
    entityId = models.UUIDField()
    entityType = models.CharField(max_length=30, choices=[
        ("word", "Word"),
        ("collection", "Collection"),
        ("quiz", "Quiz"),
    ]) #sub id
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
