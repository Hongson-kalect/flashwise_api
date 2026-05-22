from django.db import models
from uuid6 import uuid7
from ai.models.AISense import AISense

# from core.models.Collection import Collection

class CollectionItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    value= models.CharField(max_length=100, null=True, blank=True)
    collection = models.ForeignKey("core.Collection", on_delete=models.CASCADE)
    original = models.ForeignKey(AISense, on_delete=models.CASCADE, related_name='original_items', null=True) # Khóa UUIDv7 của gốc, không cho sửa thủ công
    sense = models.ForeignKey(AISense, on_delete=models.CASCADE, related_name='sense_items')
    order = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=10, null=True, blank=True) # loading, error, invalid, ok
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