from config.models import BaseModel
from django.db import models

class MonthLearnLog(BaseModel):
    """
    Tổng hợp theo tháng – monthStartDate là ngày 1 của tháng
    """
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="month_logs")
    month_start_date = models.DateField()

    words_learned = models.PositiveIntegerField(default=0)
    words_relearned = models.PositiveIntegerField(default=0)

    learn_time = models.PositiveIntegerField(default=0, help_text="Thời gian học (phút hoặc giây)")
    app_time = models.PositiveIntegerField(default=0, help_text="Tổng thời gian mở app (phút hoặc giây)") #Mỗi lần đóng app thì gửi đếm lên server

    xp_earned = models.PositiveIntegerField(default=0)
    active_days = models.PositiveIntegerField(default=0)
    sessions = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "month_log"
        ordering = ["-month_start_date"]
        unique_together = ("user", "month_start_date")
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["month_start_date"]),
        ]

    def __str__(self):
        return f"MonthLog({self.user_id}, {self.month_start_date})"
