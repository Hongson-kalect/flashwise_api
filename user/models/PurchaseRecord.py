from django.db import models
from config.models import BaseModel

class PurchaseRecord(BaseModel):
    PLATFORM_CHOICES = [
        ("google", "Google Play"),
        ("apple", "App Store"),
        ("stripe", "Stripe (Web)"), # Dự phòng nếu sau này bạn làm thanh toán qua Web
    ]

    STATUS_CHOICES = [
        ("pending", "Pending"),       # Giao dịch đang chờ xử lý (Ví dụ: Chờ Apple/Google xác thực)
        ("success", "Success"),       # Thanh toán thành công, đã kích hoạt VIP
        ("failed", "Failed"),         # Giao dịch thất bại
        ("refunded", "Refunded"),     # User đòi hoàn tiền thành công, phải hủy VIP
        ("expired", "Expired"),       # Gói subscription đã hết hạn
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='purchase_records')
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    
    # Mã định danh gói (Ví dụ: "com.flashwise.premium.1month", "com.flashwise.vip.1year")
    product_id = models.CharField(max_length=100)
    
    # 🛠️ CHÚ Ý: Google Play và Apple App Store luôn trả về ID đơn hàng duy nhất
    order_id = models.CharField(max_length=255, unique=True)
    
    # 🛠️ SỬA ĐỔI: Chuyển sang TextField vì chuỗi token này của Google rất dài
    purchase_token = models.TextField(blank=True, null=True)
    
    # 🛠️ SỬA ĐỔI: Chuyển sang TextField để chứa cục Base64 Receipt khổng lồ của Apple Store
    receipt = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    
    # Mốc thời gian
    purchased_at = models.DateTimeField(db_index=True) # Index để thống kê doanh thu theo tháng/năm nhanh
    expired_at = models.DateTimeField(null=True, blank=True) # Mốc thời gian để quét hủy VIP khi hết hạn
    
    # Tài chính (Bắt buộc dùng Decimal để tránh sai số dấu phẩy động của Float)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10, default="VND") # Ví dụ: "USD", "VND"
    discount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, default=0.00)

    # Lịch sử Webhook / Metadata từ Store gửi về phục vụ tra cứu khi có tranh chấp khiếu nại
    store_meta = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'purchase_record'
        ordering = ['-purchased_at']
        indexes = [
            # Tối ưu cho Cron Job chạy hàng đêm: Tìm các đơn hàng thành công đã quá hạn để hạ cấp VIP của user
            models.Index(fields=['status', 'expired_at']),
            # Tối ưu cho việc kiểm tra lịch sử mua hàng của một user cụ thể
            models.Index(fields=['user', '-purchased_at']),
        ]

    def __str__(self):
        return f"Order {self.order_id} ({self.platform}) - User: {self.user.email} [{self.status}]"