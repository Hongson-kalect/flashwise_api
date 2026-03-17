from django.db import models

from config.models import BaseModel
# Có thể không dùng đến

class EntityLink(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="entity_link")
    is_active = models.BooleanField(default=True)
    entityId = models.UUIDField()
    entityType = models.CharField(max_length=30, choices=[
        ("word", "Word"),
        ("collection", "Collection"),
        ("quiz", "Quiz"),
    ])

    class Meta:
        unique_together = ("entityId", "entityType")