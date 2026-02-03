from config.models import BaseModel
from django.db import models

class ViewWord(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="word_views")
    word_sub_id = models.CharField(max_length=50, db_index=True, null=True, blank=True)
    word = models.ForeignKey("core.Word", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        db_table = "word_view"
        unique_together = ("user", "word")

    def __str__(self):
        return f"WordView({self.user_id} → {self.word_id})"