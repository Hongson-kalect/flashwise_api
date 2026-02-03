from config.models import BaseModel
from django.db import models

class ViewCollection(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="collection_views")
    collection = models.ForeignKey(
        'core.Collection', on_delete=models.CASCADE, related_name="views"
    )

    class Meta:
        db_table = "collection_view"
        unique_together = ("user", "collection")

    def __str__(self):
        return f"CollectionView({self.user_id} → {self.collection_id})"
