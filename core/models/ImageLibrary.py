
from django.db import models
from django.db.models import JSONField

from utils.utils import uuidv7

class ImageLibrary(models.Model):
    id = models.UUIDField(primary_key=True, default=uuidv7.generate_uuid7, editable=False)
    description = models.CharField(max_length=255, db_index=True)
    url = models.URLField(max_length=500)
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

    class Meta:
        # Tránh một mô tả bị lặp lại cùng một version ảnh từ cùng một nguồn
        unique_together = ('description', 'version', 'provider')

    def soft_delete(self):
        self.is_deleted = True
        self.is_active = False
        self.save()

# --- Service xử lý lấy ảnh từ Unsplash ---
import requests

class ImageService:
    @staticmethod
    def fetch_and_save_unsplash(query, count=3):
        """
        Gọi Unsplash API, lấy ảnh và lưu vào ImageLibrary
        """
        UNSPLASH_ACCESS_KEY = 'your_key_here'
        url = f"https://api.unsplash.com/search/photos?query={query}&per_page={count}"
        headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
        
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            saved_images = []
            
            for index, item in enumerate(data['results']):
                img_obj, created = ImageLibrary.objects.get_or_create(
                    provider='unsplash',
                    provider_id=item['id'],
                    defaults={
                        'description': query,
                        'url': item['urls']['regular'],
                        'thumbnail_url': item['urls']['small'],
                        'version': index + 1,
                        'attribution': {
                            'name': item['user']['name'],
                            'link': item['user']['links']['html']
                        },
                        'metadata': {
                            'blurhash': item['blur_hash'],
                            'color': item['color']
                        }
                    }
                )
                saved_images.append(img_obj)
            return saved_images
        return []