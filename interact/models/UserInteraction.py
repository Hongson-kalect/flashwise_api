
from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from config.models import BaseModel

class UserInteraction(BaseModel):
    INTERACTION_CHOICES = [
        ('like', 'Like'),
        # ('download', 'Download'), sử dụng bảng userCollection thì đúng hơn
        ('bookmark', 'Bookmark'), # Có thể mở rộng sau này
    ]
    TARGET_TYPES=[
        ('collection', 'Collection'),
        ('word', 'Word'),
        ('sense', 'Sense'),
        ('quiz', 'Quiz'),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="interactions")
    
    # Định danh loại tương tác (Mặc định là 'like')
    target_type = models.CharField(max_length=20, choices=TARGET_TYPES, default='like')
    target_id = models.UUIDField(blank=True, null=True)

    # Định danh loại tương tác (Mặc định là 'like')
    interaction_type = models.CharField(max_length=20, choices=INTERACTION_CHOICES, default='like')
    status = models.BooleanField(default=True, null=True)

    class Meta:
        db_table = "user_interaction"
        # Ràng buộc Unique: 1 User chỉ được Like 1 thực thể cố định 1 lần duy nhất
        unique_together = ('user', 'interaction_type', 'target_type', 'target_id')
        
        # Tối ưu hóa Index cho việc truy vấn kiểm tra trạng thái hoặc lấy danh sách đã thích
        indexes = [
            models.Index(fields=['user', 'interaction_type', 'target_type', 'target_id']),
            models.Index(fields=['user', 'interaction_type', 'interaction_type', 'status']),
            models.Index(fields=['target_type', 'target_id']), # Phục vụ đếm tổng số lượt Like trên Server
            models.Index(fields=['interaction_type', 'status']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.interaction_type} - {self.content_type.model} ({self.object_id})"