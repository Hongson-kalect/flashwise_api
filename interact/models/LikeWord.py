from django.db import models

from config.models import BaseModel
from core.models import Word


# 1️⃣ WordLike
class LikeWord(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="word_likes")
    word = models.ForeignKey('core.Word', on_delete=models.CASCADE, related_name="likes")
    word_sub_id = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "word_like"
        unique_together = ("user", "word_sub_id")

    def __str__(self):
        return f"WordLike({self.user_id} → {self.word_sub_id})"