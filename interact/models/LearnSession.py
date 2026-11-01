from django.utils import timezone
from config.models import BaseModel
from django.db import models
from django.contrib.postgres.fields import ArrayField

class LearnSession(BaseModel):
    # Kiểu phiên, xem flashwise/docs/learning-design.md
    TYPE_CHOICES = [
        ("learn", "Learn"), # Học theo cụm 4-10 từ, mỗi từ qua nhiều bước
        ("review", "Review"), # Ôn bằng vuốt (từ cấp >= swipe_min_level)
        ("voice", "Voice"), # Đọc to, máy nhận dạng và chấm
        ("speed", "Speed"), # Chơi tính giờ, không tính FSRS
        ("mixed", "Mixed"), # Ôn rồi học mới trong cùng phiên
        ("quicklearn", "Quick Learn"), # Cũ
        ("recall", "Recall"), # Cũ
    ]
    STATUS_CHOICES = [
        ("in_progress", "In progress"),
        ("completed", "Completed"),
        ("abandoned", "Abandoned"),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="learn_sessions")
    start_at = models.DateTimeField(default=timezone.now)
    end_at = models.DateTimeField(blank=True, null=True)
    senses = ArrayField(base_field=models.CharField(max_length=50), blank=True, default=list) #word subId or user Word id 
    sense_count = models.PositiveIntegerField(default=0)
    time = models.PositiveIntegerField(default=0)  # tổng thời gian (giây)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="learn")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="completed")
    collection_id = models.UUIDField(null=True, blank=True) # Bộ từ đang học (null = học từ lẻ / hàng đợi ôn chung)
    methods = models.JSONField(default=list, blank=True) # Mã phương pháp đã bật trong phiên, vd ["write", "cloze", "listen"]
    new_count = models.PositiveIntegerField(default=0) # Số từ mới
    review_count = models.PositiveIntegerField(default=0) # Số từ ôn
    correct_count = models.PositiveIntegerField(default=0) # Số lần trả lời đúng (mọi bước)
    wrong_count = models.PositiveIntegerField(default=0)
    xp_earned = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "learn_session"
        ordering = ["-start_at"]
        indexes = [
            models.Index(fields=["user", "-start_at"], name="idx_session_user_time"),
        ]

    def __str__(self):
        return f"LearnSession({self.user_id}, {self.type}, {self.start_at:%Y-%m-%d})"
