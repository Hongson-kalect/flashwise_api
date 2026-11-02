from config.models import BaseModel
from django.db import models

class Tag(BaseModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    languageCode = models.CharField(max_length=10, blank=True, null=True)
    isSystemTag = models.BooleanField(default=False) # Chủ đề hệ thống: hiện ở màn khởi động để user chọn chủ đề quan tâm
    names = models.JSONField(default=dict, blank=True) # Tên theo ngôn ngữ giao diện {vi: 'Du lịch', en: 'Travel'}; thiếu thì dùng name
    display_order = models.IntegerField(default=0)

    def __str__(self):
        return self.name