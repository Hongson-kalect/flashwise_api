
from django.db import models
from config.models import BaseModel

class AISenseMetadata(BaseModel):
    # Nâng max_length để né lỗi chặt cụt link ảnh có token dài
    image_link = models.URLField(max_length=1000, null=True, blank=True)
    
    # Giữ nguyên cấu trúc JSON linh hoạt cho Server
    advanced = models.JSONField(default=dict, null=True, blank=True) # collocation, idiom, synonyms, antonyms, relateds, forms
    tags = models.JSONField(default=list, blank=True)
    image_keywords = models.JSONField(null=True, blank=True, default=list)
    image_metadata = models.JSONField(default=dict, null=True, blank=True)  # {width, height, size, format}

    class Meta:
        db_table = "sense_metadata"
        ordering = ["-created_at"]
        indexes = [
            # Thêm index cho hành động kiểm tra hoặc quét tìm các bản ghi có hình ảnh
            models.Index(fields=["image_link"], name="idx_meta_image_link"),
            models.Index(fields=["created_at"]),
        ]
        