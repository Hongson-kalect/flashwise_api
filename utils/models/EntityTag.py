from django.db import models

from config.models import BaseModel


class EntityTag(BaseModel):
    tag = models.ForeignKey('utils.Tag', on_delete=models.CASCADE, related_name="entity_tags")
    entityId = models.UUIDField()
    entityType = models.CharField(max_length=30, choices=[
        ("word", "Word"),
        ("collection", "Collection"),
        ("quiz", "Quiz"),
    ])

    class Meta:
        unique_together = ("tag", "entityId", "entityType")