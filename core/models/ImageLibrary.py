
from venv import create
from django.db import models
from django.db.models import JSONField
from django.core.files.storage import FileSystemStorage
from django.conf import settings
import os

from utils.utils import uuidv7

temp_storage = FileSystemStorage(location=os.path.join(settings.MEDIA_ROOT, 'images'))

class ImageLibrary(models.Model):
    id = models.UUIDField(primary_key=True, default=uuidv7.generate_uuid7, editable=False)

    file = models.ImageField(storage=temp_storage, upload_to='v1/', null=True, blank=True)
    url = models.URLField(max_length=1000, null=True, db_index=True) # Tăng max_length đề phòng CDN link dài
    thumbnail_url = models.URLField(max_length=1000, null=True, blank=True)
    version = models.IntegerField(default=1)
    
    is_active = models.BooleanField(default=True, db_index=True)
    is_public = models.BooleanField(default=True, db_index=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    
    provider = models.CharField(max_length=50, default='unsplash') 
    provider_id = models.CharField(max_length=100, null=True, blank=True)
    contributor = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    
    metadata = JSONField(default=dict, blank=True) 
    attribution = JSONField(default=dict, blank=True) 
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def save(self, *args, **kwargs):
        if self.file and not self.url:
            self.url = self.file.url
        super().save(*args, **kwargs)

    class Meta:
        db_table = "image_library"
        ordering = ["-created_at"]
        constraints = [
            # Chặn trùng lặp chuẩn xác cho ảnh từ Unsplash/AI, bỏ qua ảnh user (khi provider_id bị null)
            models.UniqueConstraint(
                fields=['provider', 'provider_id'],
                condition=models.Q(provider_id__isnull=False),
                name='unique_provider_image_id'
            )
        ]

    def soft_delete(self):
        self.is_deleted = True
        self.is_active = False
        self.save()

# --- Service xử lý lấy ảnh từ Unsplash ---

class ImageContext(models.Model):
    # Quan hệ N-1: Nhiều ngữ cảnh có thể dùng chung 1 ảnh vật lý
    images = models.ManyToManyField(
        ImageLibrary, 
        through='ImageLibraryContext', 
        related_name='contexts'
    )
    
    # Description tiếng Anh dùng để search local
    # Đánh index để tra cứu từ điển cực nhanh
    description = models.CharField(max_length=255, db_index=True)
    is_active = models.BooleanField(default=True)  # Hiện/Ẩn trong hệ thống
    
    provider = models.CharField(max_length=50, default='unsplash') # unsplash, user, ai
    provider_id = models.CharField(max_length=100, null=True, blank=True)
    contributor = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True) 
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Một ảnh không nên có 2 bản ghi cùng 1 description trùng lặp
        unique_together = ('description', 'provider')

    def __str__(self):
        return f"{self.provider} - {self.description}"

class ImageLibraryContext(models.Model):
    """BẢNG PHỤ THỰC SỰ: Liên kết Ngữ cảnh và Ảnh vật lý"""
    image = models.ForeignKey(ImageLibrary, on_delete=models.CASCADE)
    context = models.ForeignKey(ImageContext, on_delete=models.CASCADE)
    
    # Các trường metadata cho mối quan hệ
    order = models.IntegerField(default=0) # Thứ tự ưu tiên hiển thị (0-4)
    added_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Quan trọng: Không cho phép trùng cặp (ảnh - ngữ cảnh)
        unique_together = ('image', 'context')
        ordering = ['order', '-created_at']

        indexes = [
            # Tối ưu hóa cho các câu lệnh query ảnh kèm thứ tự của một ngữ cảnh cụ thể
            models.Index(fields=['context', 'order'], name='idx_ctx_order'),
        ]