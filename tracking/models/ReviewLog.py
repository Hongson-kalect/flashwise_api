from django.db import models
from django.utils import timezone
from config.models import UUIDModel


class ReviewLog(UUIDModel):
    """
    Lưu từng lần user chấm 1 thẻ (append-only, không sửa).
    Dùng để tối ưu tham số FSRS riêng cho từng user và để tính lại progress khi cần.
    """
    RATING_CHOICES = [
        (1, "Again"),
        (2, "Hard"),
        (3, "Good"),
        (4, "Easy"),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="review_logs")
    original_sense_id = models.UUIDField()
    sense_id = models.UUIDField(null=True, blank=True) # Bản sense user đang học lúc chấm (có thể là bản custom)
    session_id = models.UUIDField(null=True, blank=True) # LearnSession chứa lần ôn này

    rating = models.SmallIntegerField(choices=RATING_CHOICES)
    mode = models.CharField(max_length=40, blank=True, default="flashcard") # <kiểu phiên>:<phương pháp>, vd learn:cloze, review:swipe, voice:speak
    # True: lần chấm đưa vào FSRS (1 dòng / thẻ / phiên). False: bước trong phiên học cụm, lần thẻ quay lại trong phiên (chỉ thống kê)
    is_fsrs = models.BooleanField(default=True)
    level = models.SmallIntegerField(default=0) # Cấp thẻ lúc chấm
    hint_count = models.SmallIntegerField(default=0) # Số gợi ý đã dùng
    attempts = models.SmallIntegerField(default=1) # Số lần trả lời tới khi xong bước
    duration_ms = models.PositiveIntegerField(default=0) # Thời gian suy nghĩ trước khi trả lời

    # Trạng thái FSRS trước và sau khi chấm
    state = models.SmallIntegerField(default=0)
    elapsed_days = models.FloatField(default=0) # Số ngày kể từ lần ôn trước
    scheduled_days = models.FloatField(default=0) # Số ngày đã hẹn ôn ở lần trước
    stability = models.FloatField(default=0)
    difficulty = models.FloatField(default=0)

    reviewed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "review_log"
        ordering = ["-reviewed_at"]
        indexes = [
            models.Index(fields=["user", "-reviewed_at"], name="idx_review_user_time"),
            models.Index(fields=["user", "original_sense_id", "reviewed_at"], name="idx_review_user_sense"),
        ]

    def __str__(self):
        return f"Review({self.user_id} -> {self.original_sense_id}, rating={self.rating})"
