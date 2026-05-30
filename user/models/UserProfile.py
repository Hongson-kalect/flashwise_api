from django.db import models
from config.models import BaseModel

class UserProfile(BaseModel):
    # ID thừa kế từ BaseModel (UUIDv7 dạng Text)

    # 🛠️ TINH CHỈNH: OneToOneField đã tự tạo UNIQUE INDEX, không cần khai báo index thủ công nữa
    user = models.OneToOneField(
        'auth.User',
        on_delete=models.CASCADE,
        related_name="profile",
    )
    
    # --- THÔNG TIN CÁ NHÂN CƠ BẢN ---
    full_name = models.CharField(max_length=255, blank=True)
    avatar_url = models.URLField(max_length=512, blank=True)
    age = models.PositiveIntegerField(null=True, blank=True)
    
    GENDER_CHOICES = [
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
    ]
    gender = models.CharField(max_length=20, choices=GENDER_CHOICES, blank=True)
    dob = models.DateField(null=True, blank=True) # Giữ dob, xóa birth_date
    country_code = models.CharField(max_length=20, blank=True)
    
    # --- CÀI ĐẶT NGÔN NGỮ VÀ HỌC TẬP (Cốt lõi Flashwise) ---
    native_language = models.CharField(max_length=50, blank=True) # Ví dụ: "vi"
    # Lưu danh sách ngôn ngữ đang học, ví dụ: ["en", "ja"]
    learning_languages = models.JSONField(default=list, blank=True) 
    # Giờ vàng học tập được cá nhân hóa để đẩy Push Notification nhắc nhở
    fav_time = models.TimeField(null=True, blank=True) 

    # --- HỆ THỐNG PHÂN HẠNG VÀ GAMIFICATION ---
    level = models.IntegerField(default=1) # Cấp độ Gamification dựa trên tổng XP thu hoạch
    # --- HỆ THỐNG GAMIFICATION & THỐNG KÊ (HẤP THỤ TỪ STAT) ---
    level = models.IntegerField(default=1, db_index=True)
    total_xp = models.PositiveIntegerField(default=0, db_index=True) # Index để xếp hạng tổng hành tinh
    current_streak = models.PositiveIntegerField(default=0, db_index=True) # Index để xếp hạng thánh cày cuốc
    max_streak = models.PositiveIntegerField(default=0)
    
    TIER_CHOICES = [
        ("guest", "Guest"),              # Tài khoản khách chơi thử, giới hạn tính năng
        ("normal_user", "Normal User"),  # User đăng ký chính thức miễn phí
        ("VIP", "VIP User"),             # Gói trả phí Premium
        ("admin", "Admin/Staff"),        # Quản trị viên
    ]
    tier = models.CharField(max_length=20, choices=TIER_CHOICES, default="guest")
    tier_expired_at = models.DateTimeField(null=True, blank=True) # Thời hạn gói VIP

    # --- CẤU HÌNH TIMEZONE PHỤC VỤ CHẠY CRON JOB ---
    time_zone = models.CharField(max_length=100, blank=True, default="Asia/Ho_Chi_Minh")
    zone_num = models.IntegerField(null=True, blank=True, default=7) # Ví dụ: UTC +7

    # --- AN NINH HỆ THỐNG (Đã gộp từ BanList) ---
    is_active = models.BooleanField(default=True) # Quản lý trạng thái chặn/mở tài khoản nhanh
    ban_reason = models.TextField(blank=True, null=True)
    ban_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "user_profile"
        indexes = [
            # Chỉ mục này cực kỳ quan trọng cho các câu lệnh quét Background Job trên Server:
            # Ví dụ: Tìm tất cả user ở múi giờ UTC+7 có fav_time = 20:00 để chuẩn bị bắn Push Notification nhắc học
            models.Index(fields=["zone_num", "fav_time"]),
            
            # Phục vụ tính năng thống kê, BXH theo quốc gia
            models.Index(fields=["country_code"]),
        ]

    def __str__(self):
        return f"Profile of {self.full_name or self.user.email} (Tier: {self.tier})"