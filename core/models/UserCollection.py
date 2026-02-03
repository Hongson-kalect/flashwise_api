from operator import index
from django.db import models

from config.models import BaseModel

class UserCollection(BaseModel):
    sub_id = models.CharField(max_length=100, blank=True, null=True, db_index=True) # uuid v7
    collection = models.ForeignKey(
        'core.Collection', on_delete=models.CASCADE, related_name="user_collections", null=True
    )
    added_sense = models.JSONField(default=list, blank=True, null=True)
    removed_sense = models.JSONField(default=list, blank=True, null=True)
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="downloaded_collections")
    sync = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=['is_active', 'is_deleted', '-created_at'], name='downloaded_collection_idx'),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f"UserCollection({self.user_id} → {self.collection_id})"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)