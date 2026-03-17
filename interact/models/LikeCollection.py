from config.models import BaseModel


from django.db import models
from django.contrib.auth import get_user_model

from core.models import Collection

User = get_user_model()

class LikeCollection(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="collection_likes")
    collection = models.ForeignKey(
       'core.Collection', on_delete=models.CASCADE, related_name="likes", null=True, blank=True
    )
    collection_sub_id = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "collection_like"
        unique_together = ("user", "collection_sub_id")

    def __str__(self):
        return f"Like({self.user_id} → {self.collection_sub_id})"