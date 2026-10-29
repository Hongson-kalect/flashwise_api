from django.db import models
from config.models.UUIDModel import UUIDModel

class MostLikeWord(UUIDModel):
    """
    Dữ liệu top từ được yêu thích (được tính toán định kỳ).
    Dùng để hiển thị trong trang Discover hoặc tab xu hướng.
    """
    language = models.ForeignKey('core.Language', on_delete=models.CASCADE)
    word = models.ForeignKey('ai.AIWord', on_delete=models.CASCADE)
    like_count = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    cached_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "most_liked_words"
