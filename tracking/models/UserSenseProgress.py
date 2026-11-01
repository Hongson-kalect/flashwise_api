from django.db import models
from config.models import UUIDModel


class UserSenseProgress(UUIDModel):
    """
    Tiến độ học 1 nghĩa của 1 user. Bản gốc nằm ở SQLite trên máy (local-first),
    bảng này là bản sync lên server -> tên trường giữ giống bảng user_sense_progress phía app.
    Thuật toán lặp: FSRS (stability / difficulty / state).
    """
    STATE_CHOICES = [
        (0, "New"),
        (1, "Learning"),
        (2, "Review"),
        (3, "Relearning"),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="sense_progresses")
    # Gắn với original_id để dù từ có nâng cấp version mới thì tiến trình học của user không bị mất
    original_sense_id = models.UUIDField(db_index=True)

    # --- Trạng thái hiển thị cho UI ---
    # Cấp thẻ 0-7: quyết định kiểu thẻ / phương pháp học (0 = thẻ đầy đủ lần đầu, >= 3 thì ôn bằng vuốt...), xem flashwise/docs/learning-design.md
    level = models.IntegerField(default=0)
    max_level = models.IntegerField(default=0) # Cấp cao nhất từng đạt, cho phép lên lại nhanh khi học lại từ đã quên

    # --- Bộ đếm học tập ---
    streak_correct = models.IntegerField(default=0) # Số lần nhớ liên tiếp
    forget_count = models.IntegerField(default=0) # Số lần bấm "Quên" (FSRS: lapses)
    remember_count = models.IntegerField(default=0) # Số lần bấm "Nhớ"
    repetitions = models.IntegerField(default=0) # Tổng số lần ôn (FSRS: reps)

    # --- Chỉ số FSRS ---
    state = models.SmallIntegerField(choices=STATE_CHOICES, default=0)
    stability = models.FloatField(default=0) # Số ngày để xác suất nhớ giảm còn 90%
    difficulty = models.FloatField(default=0) # 1 - 10, 0 = chưa học lần nào
    scheduled_days = models.FloatField(default=0) # Khoảng ôn đã hẹn ở lần chấm gần nhất (ngày)
    learning_steps = models.IntegerField(default=0) # Bước học ngắn hạn hiện tại (ts-fsrs: learning_steps)
    failed_at = models.DateTimeField(null=True, blank=True) # Lần quên gần nhất

    # --- Cờ lọc trạng thái nhanh ---
    is_mastered = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)
    is_avoid = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_reviewed_at = models.DateTimeField(null=True, blank=True)
    next_review_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True) # Mốc so sánh khi sync 2 chiều
    client_updated_at = models.DateTimeField(null=True, blank=True) # Giờ sửa theo máy client, dùng để chọn bản mới hơn khi sync (updated_at do server gán nên không so được)

    class Meta:
        db_table = "user_sense_progress"
        constraints = [
            models.UniqueConstraint(fields=["user", "original_sense_id"], name="unique_user_sense_progress"),
        ]
        indexes = [
            # Hàng đợi ôn tập: các từ đến hạn của 1 user, bỏ qua mastered/hidden/avoid
            models.Index(
                fields=["user", "next_review_at"],
                name="idx_progress_due",
                condition=models.Q(is_mastered=False, is_hidden=False, is_avoid=False, is_deleted=False),
            ),
            models.Index(fields=["user", "updated_at"], name="idx_progress_sync"),
        ]

    def __str__(self):
        return f"Progress({self.user_id} -> {self.original_sense_id}, state={self.state})"
