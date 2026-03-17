from django.db import models

from config.models import BaseModel


# 1️⃣ WordLike
class ForgetWord(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="forget_words")
    word = models.ForeignKey('core.Word', on_delete=models.CASCADE, related_name="forget")
    word_sub_id = models.CharField(max_length=50)

    class Meta:
        db_table = "forget_word"

    def __str__(self):
        return f"Forget word({self.user_id} → {self.word_sub_id})"