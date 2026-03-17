
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

    file = models.ImageField(storage=temp_storage, upload_to='v1/', null=True)
    url = models.URLField(max_length=500, null=True)
    thumbnail_url = models.URLField(max_length=500, null=True, blank=True)
    version = models.IntegerField(default=1)
    
    # Quản lý trạng thái
    is_active = models.BooleanField(default=True)  # Hiện/Ẩn trong hệ thống
    is_public = models.BooleanField(default=True)  # Cộng đồng dùng chung hay cá nhân
    is_deleted = models.BooleanField(default=False) # Soft delete
    
    # Nguồn và định danh
    provider = models.CharField(max_length=50, default='unsplash') # unsplash, user, ai
    provider_id = models.CharField(max_length=100, null=True, blank=True)
    contributor = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    
    # Metadata & Credit
    metadata = JSONField(default=dict, blank=True) # {blurhash, width, height, size}
    attribution = JSONField(default=dict, blank=True) # {author_name, author_link, site_link}
    
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Tự động cập nhật URL mỗi khi lưu
        if self.file:
            self.url = self.file.url
        super().save(*args, **kwargs)

    class Meta:
        # Tránh một mô tả bị lặp lại cùng một version ảnh từ cùng một nguồn
        unique_together = ('provider_id', 'provider')

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