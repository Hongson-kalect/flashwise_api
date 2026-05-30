from django.db import models
from config.models import BaseModel

class UserSetting(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='settings')
    
    # Mã cài đặt (Ví dụ: "theme", "app_language", "notification_enabled", "daily_target")
    key = models.CharField(max_length=100)
    
    # 🛠️ LINH HOẠT: Lưu value dưới dạng JSONField để chứa được cả chuỗi, số, boolean hoặc object phức tạp
    value = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "user_setting"
        
        # 🛠️ RÀNG BUỘC: Một user không thể có 2 dòng cấu hình cho cùng 1 key
        unique_together = ('user', 'key')
        
        indexes = [
            # Tối ưu cho luồng load toàn bộ cài đặt của 1 user cụ thể
            models.Index(fields=["user", "key"]),
        ]

    def __str__(self):
        return f"{self.user.email} | {self.key} = {self.value}"
    
    # const INITIAL_SETTINGS = [
    #   ["theme", "system"],
    #   ["app_language", "vi"],
    #   ["vibration_enabled", true],
    #   ["auto_play_audio", true],
    #   ["audio_voice_gender", "female"],
    #   ["audio_speed", 1.0],
    #   ["sound_effects", true],
    #   ["preferred_learning_mode", "mixed"],
    #   ["daily_target_cards", 20],
    #   ["srs_review_reminder", true],
    #   ["push_enabled", true],
    #   ["fav_learning_time", "20:00"],
    #   ["streak_remind_enabled", true]
    # ];