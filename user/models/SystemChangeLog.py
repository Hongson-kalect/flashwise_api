from django.db import models
from config.models import BaseModel

class SystemChangeLog(BaseModel):
    # Tên bảng hệ thống bị thay đổi (Ví dụ: 'core_word', 'core_sense', 'system_collection')
    table_name = models.CharField(max_length=50, db_index=True)
    
    # ID của bản ghi hệ thống được cập nhật
    record_id = models.CharField(max_length=255, db_index=True)
    
    # Hành động: 'UPSERT' (Thêm/Sửa) hoặc 'DELETE' (Xóa bỏ khỏi kho hệ thống)
    action = models.CharField(max_length=10, choices=[('UPSERT', 'Upsert'), ('DELETE', 'Delete')])
    
    # 🛠️ CỐT LÕI: Mốc thời gian chuẩn do Server đóng dấu lúc thực hiện thay đổi
    # Khi client gửi lên 'last_sync', Server sẽ tìm các bản ghi có server_changed_at > last_sync
    server_changed_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = 'system_change_log'
        ordering = ['server_changed_at']

    def __str__(self):
        return f"System Change -> {self.table_name} [{self.action}] at {self.server_changed_at}"