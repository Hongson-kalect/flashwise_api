
from config.models import BaseModel
from django.db import models

class WeekLearnLog(BaseModel):
    """
    Tổng hợp theo tuần – tuần bắt đầu từ Monday
    """
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="week_logs")
    week_start_date = models.DateField()

    words_learned = models.PositiveIntegerField(default=0)
    words_relearned = models.PositiveIntegerField(default=0)

    learn_time = models.PositiveIntegerField(default=0, help_text="Thời gian học (phút hoặc giây)")
    app_time = models.PositiveIntegerField(default=0, help_text="Tổng thời gian mở app (phút hoặc giây)") #Mỗi lần đóng app thì gửi đếm lên server

    xp_earned = models.PositiveIntegerField(default=0)
    sessions =  models.PositiveIntegerField(default=0) # Nếu muốn lấy chi tiết thì query bảng LearnSession là ra
    active_days = models.PositiveIntegerField(default=0)



    class Meta:
        db_table = "week_log"
        ordering = ["-week_start_date"]
        unique_together = ("user", "week_start_date")
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["week_start_date"]),
        ]

    def __str__(self):
        return f"WeekLog({self.user_id}, {self.week_start_date})"

