from django.db import models
from config.models.UUIDModel import UUIDModel

class MostForgetWord(UUIDModel):
    """
    Thống kê những nghĩa mà người dùng (toàn hệ thống) hay quên nhất.
    -> Hỗ trợ hiển thị “Những từ đáng ôn lại” hoặc bảng từ gợi ý.
    Tính theo sense vì 1 từ có nhiều nghĩa, độ khó mỗi nghĩa khác nhau.
    """
    language = models.ForeignKey('core.Language', on_delete=models.CASCADE)
    sense = models.ForeignKey('ai.AISense', on_delete=models.CASCADE)
    forgot_count = models.PositiveIntegerField(default=0)
    total_count = models.PositiveIntegerField(default=0)
    cached_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "most_forgot_words"
