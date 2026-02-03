
from django.utils import timezone
from email.policy import default
from config.models import BaseModel
from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import ArrayField

User = get_user_model()

class DayLearnLog(BaseModel):
    """
    Tổng hợp theo ngày – dùng cho dashboard, streak, heatmap,...
    """
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="daily_logs")
    date = models.DateField(default=timezone.now)
    words_learned = models.PositiveIntegerField(default=0)
    words_relearned = models.PositiveIntegerField(default=0)

    learn_time = models.PositiveIntegerField(default=0, help_text="Thời gian học (phút hoặc giây)")
    app_time = models.PositiveIntegerField(default=0, help_text="Tổng thời gian mở app (phút hoặc giây)") #Mỗi lần đóng app thì gửi đếm lên server

    xp_earned = models.PositiveIntegerField(default=0)
    sessions =  models.PositiveIntegerField(default=0) # Nếu muốn lấy chi tiết thì query bảng LearnSession là ra

    #  Những dữ liệu trên đều dễ lấy nếu query bảng LearnSession nhưng nếu làm dashboard thì sẽ phải xử lý 1 lúc => Cần các dữ liệu chính để dashboard

    class Meta:
        db_table = "daily_log"
        ordering = ["-date"]
        unique_together = ("user", "date")
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["date"]),
        ]

    def __str__(self):
        return f"DailyLog({self.user_id}, {self.date})"