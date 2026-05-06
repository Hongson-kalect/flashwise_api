from django.db import models
from config.models import BaseModel
from django.utils import timezone

class UserSenseProcess(models.Model):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    # Gắn với original_id để dù từ có nâng cấp version mới thì tiến trình học của user không bị mất
    original_sense_id = models.UUIDField(db_index=True) 
    
    # --- Các trường quản lý học tập cá nhân nằm ở ĐÂY ---
    level = models.IntegerField(default=0) # Trạng thái học (0: Unknown, 1: New, 2: Learning, 3: Review, 4: Mastered...)
    max_level = models.IntegerField(default=0) # Dùng để cho phép quay lại rank nhanh hơn các từ mới trong trường hợp học lại
    streak_correct = models.IntegerField(default=0) # Số lần nhớ liên tiếp
    forget_count = models.IntegerField(default=0) # Số lần bấm "Quên"
    remember_count = models.IntegerField(default=0) # Số lần bấm "Quên"
    
    # --- Phục vụ thuật toán Spaced Repetition (Anki/SuperMemo) ---
    easiness_factor = models.FloatField(default=2) # Hệ số sảnh (E-Factor) 1.3-2.5
    easiness_level = models.SmallIntegerField(default=100) # Độ khó của từ đối với user, mặc định lấy sense.metadata.cefr để quy đổi, ảnh hưởng đến e_factor tăng và giảm (E-Level)
    repetitions = models.IntegerField(default=0)
    next_review_date = models.DateTimeField(null=True, blank=True)
    last_reviewed_at = models.DateTimeField(null=True, blank=True)