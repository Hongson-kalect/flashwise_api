from django.db import models

class InterestingWord(models.Model):
    """
    Lưu các từ được người dùng xem nhiều nhất trong hệ thống.
    -> Dùng cho phần Discover, Top search, hoặc để gợi ý xu hướng.
    """
    language = models.ForeignKey('core.Language', on_delete=models.CASCADE)
    word = models.ForeignKey('core.Word', on_delete=models.CASCADE)
    word_sub_id = models.CharField(max_length=50)
    view_count = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    last_view_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "interested_words"