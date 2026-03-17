from django.db import models

class MostForgetWord(models.Model):
    """
    Thống kê những từ mà người dùng (toàn hệ thống) hay quên nhất.
    -> Hỗ trợ hiển thị “Những từ đáng ôn lại” hoặc bảng từ gợi ý.
    """
    language = models.ForeignKey('core.Language', on_delete=models.CASCADE)
    word = models.ForeignKey('core.Word', on_delete=models.CASCADE)
    word_sub_id = models.CharField(max_length=50)
    forgot_count = models.PositiveIntegerField(default=0)
    total_count = models.PositiveIntegerField(default=0)
    cached_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "most_forgot_words"