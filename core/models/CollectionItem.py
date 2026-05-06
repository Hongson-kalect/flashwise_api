from django.db import models
from uuid6 import uuid7
from ai.models.AISense import AISense
from config.models import BaseModel
from django.contrib.postgres.indexes import GinIndex

from core.models.Collection import Collection

class CollectionItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE)
    original_id = models.UUIDField(null=True) # Khóa UUIDv7 của gốc, không cho sửa thủ công
    sense = models.ForeignKey(AISense, on_delete=models.CASCADE)
    order = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'collection_items'
        # Ràng buộc Unique để tránh 1 từ xuất hiện 2 lần trong 1 bộ
        unique_together = ('collection', 'sense')

        # Tối ưu hóa Index cho việc truy vấn danh sách từ của 1 bộ
        indexes = [
            models.Index(fields=['collection','order']),
            models.Index(fields=['sense', 'collection'])
        ]
        ordering = ['order', 'created_at']