from django.db import models

from core.models import Collection, Language

class MostLikeCollection(models.Model):
    """
    Dữ liệu top collection được yêu thích (được tính toán định kỳ).
    Dùng để hiển thị trong trang Discover hoặc tab xu hướng.
    """
    language = models.ForeignKey('core.Language', on_delete=models.CASCADE)
    collection = models.ForeignKey('core.Collection', on_delete=models.CASCADE)
    collection_sub_id = models.CharField(max_length=50)
    like_count = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    cached_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "most_liked_collections"