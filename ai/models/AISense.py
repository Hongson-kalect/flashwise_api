

from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AIWord import AIWord
from config.models import BaseModel
from django.db import models
from django.contrib.postgres.fields import ArrayField

class AISense(BaseModel):
    word = models.ForeignKey(AIWord, related_name='senses', on_delete=models.CASCADE)
    word_value = models.TextField(null=True, blank=True) # Hiện luôn value để đỡ phải join
    original = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='original_sense') # Bản gốc, dùng để compare hiển thị cho người dùng (unique original - Không có thì giời biết 2 bản có phải cùng 1 sense không)
    previous = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='previous_sense') # Bản trước đó, dùng để lấy dữ liệu frozen
    origins = models.JSONField(default=list) # [id1, id2...]
    metadata = models.ForeignKey(AISenseMetadata, on_delete=models.SET_NULL, null=True, related_name='senses')
    preview =models.JSONField(default=dict) # image url, định nghĩa, audio_url nếu cần
    image_preview = models.URLField(max_length=1000, null=True, blank=True)
    image_context = models.ForeignKey('core.ImageContext', on_delete=models.SET_NULL, null=True, blank=True)
    language_code = models.CharField(max_length=10, blank=True, null=True)
    contents = models.JSONField(default=dict, null=True, blank=True)
    delta = models.JSONField(default=dict, null=True, blank=True)

    pos = models.CharField(max_length=20, blank=True, null=True)
    level = models.CharField(max_length=20, blank=True, null=True)
    register = ArrayField(default=list, base_field=models.CharField(max_length=50))
    ipas = models.JSONField(default=list) # {label, audio, value, reading, roman, ruby}

    voted = models.IntegerField(default=0)
    devoted = models.IntegerField(default=0)
    likes = models.IntegerField(default=0)
    views = models.IntegerField(default=0)


    is_offensive = models.BooleanField(default=False, null=True)
    is_frozen = models.BooleanField(default=True, null=True)

    versions = models.IntegerField(default=1)
    # True = can't update, always create new. False = can update news content. None = can update all
    # When update content not in news with is_frozen = False, create new content, replace in contents and update news 

    is_official = models.BooleanField(default=True)
    is_ai_created = models.BooleanField(default=True)
    score = models.IntegerField(default=0, db_index=True)
    like_count = models.IntegerField(default=0)
    main_count = models.IntegerField(default=0)

    global_forget_count = models.IntegerField(default=0)   # Tổng số lần TẤT CẢ user bấm "Quên" từ này khi học
    global_remember_count = models.IntegerField(default=0) # Tổng số lần TẤT CẢ user bấm "Thuộc/Nhớ" từ này

    class Meta:
        db_table = "ai_sense"
        ordering = ["-created_at"]
        indexes = [
            # INDEX CHÍ MẠNG TỐI ƯU API: Lấy các sense active, sạch sẽ của 1 từ cụ thể
            models.Index(fields=["word", "is_active", "is_deleted", "is_official"], name="idx_sense_core_lookup"),
            # Index phục vụ bộ lọc tìm kiếm nâng cao theo ngôn ngữ, từ loại và trình độ
            models.Index(fields=["language_code", "pos", "level"], name="idx_sense_filter"),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"Sense ({self.word_value} - {self.pos or 'Unknown'})"