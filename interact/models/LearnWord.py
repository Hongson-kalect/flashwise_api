from django.db import models

from config.models import BaseModel


# 1️⃣ WordLike
class LearnWord(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="learn_words")
    word = models.ForeignKey('core.Word', on_delete=models.CASCADE, related_name="learn")
    word_sub_id = models.CharField(max_length=50)

    class Meta:
        db_table = "learn_word"

    def __str__(self):
        return f"Learn Like({self.user_id} → {self.word_sub_id})"