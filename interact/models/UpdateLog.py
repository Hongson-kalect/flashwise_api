from django.db import models
from config.models import BaseModel

class UpdateLog(BaseModel):
    TYPE_CHOICES = [
        ("word", "Word"), 
        ("sense", "Sense"), 
        ("collection", "Collection"), 
        ("note", "Note")
    ]

    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    
    # 🛠️ SỬA ĐỔI: Chuyển sang UUIDField để khớp nhất quán với cấu trúc UUIDv7 toàn hệ thống
    target_id = models.UUIDField()
    
    # Dữ liệu snapshot hiện tại hoặc các thông tin bổ sung đi kèm (nếu cần)
    value = models.JSONField(default=dict, blank=True)

    # Người thực hiện yêu cầu thay đổi
    request_by = models.ForeignKey(
        'auth.User', 
        on_delete=models.CASCADE, 
        related_name="update_request_logs", 
        null=True, 
        blank=True
    )
    
    # Cấu trúc lưu vết Audit chuẩn
    change_from = models.JSONField(default=dict, blank=True)  # Trạng thái JSON cũ trước khi sửa
    change_to = models.JSONField(default=dict, blank=True)    # Trạng thái JSON mới sau khi sửa

    class Meta:
        db_table = "update_log"
        ordering = ["-created_at"]
        indexes = [
            # INDEX CỐT LÕI: Giúp Admin truy vấn cực nhanh lịch sử chỉnh sửa của MỘT từ cụ thể
            # Câu lệnh: UpdateLog.objects.filter(type='word', target_id=uuid)
            models.Index(fields=['type', 'target_id']),
            models.Index(fields=['request_by', '-created_at']), # Xem lịch sử đóng góp/sửa đổi của 1 User
        ]

    def __str__(self):
        # SỬA LỖI: Gọi đúng trường target_id
        return f"UpdateLog({self.type}:{self.target_id} by {self.request_by_id})"