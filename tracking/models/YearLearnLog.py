from config.models import BaseModel
from django.db import models


class YearLearnLog(BaseModel):
    """
    Tổng hợp theo năm – chứa thống kê toàn bộ năm
    """
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="year_logs")
    year = models.PositiveIntegerField()

    words_learned = models.PositiveIntegerField(default=0)
    words_relearned = models.PositiveIntegerField(default=0)

    learn_time = models.PositiveIntegerField(default=0, help_text="Thời gian học (phút hoặc giây)")
    app_time = models.PositiveIntegerField(default=0, help_text="Tổng thời gian mở app (phút hoặc giây)") #Mỗi lần đóng app thì gửi đếm lên server

    xp_earned = models.PositiveIntegerField(default=0)
    active_days = models.PositiveIntegerField(default=0)
    sessions = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "year_log"
        ordering = ["-year"]
        unique_together = ("user", "year")
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["year"]),
        ]

    def __str__(self):
        return f"YearLog({self.user_id}, {self.year})"