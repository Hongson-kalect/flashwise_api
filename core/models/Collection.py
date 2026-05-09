from django.db import models
from config.models import BaseModel
from django.contrib.postgres.indexes import GinIndex
from core.models.CollectionItem import CollectionItem

class Collection(BaseModel):
    sub_id = models.CharField(max_length=100, blank=True, null=True, db_index=True) # uuid v7
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    image = models.FileField(upload_to='images/collections',null=True, blank=True)
    score = models.IntegerField(default=0, null=True) #  có thể bằng uy tín người đăng.
    image_url = models.TextField(blank=True, null=True) #Đã dùng bảng image liên kết với bảng, chủ động add image khi post
    tags = models.ManyToManyField('utils.Tag', blank=True)
    is_frozen = models.BooleanField(default=False)
    is_uploaded = models.BooleanField(default=False)
    language_code = models.CharField(max_length=10, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_official = models.BooleanField(default=False)
    senses = models.ManyToManyField('ai.AISense', through=CollectionItem, 
                                    through_fields=('collection', 'sense'),
                                    blank=True)

    # update_requests = models.ManyToManyField('update_request', blank=True)
    # update_logs = models.ManyToManyField('update_log', blank=True)

    class Meta:
        db_table = 'collection'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['is_active']),
            models.Index(fields=['is_deleted']),
            models.Index(fields=['is_official']),
            models.Index(fields=['is_active', 'is_deleted', '-score']),
            models.Index(fields=['is_active', 'is_deleted', '-created_at']),
            GinIndex(fields=['name'],name = 'collection_name_trgm',opclasses=['gin_trgm_ops']),
        ]
    def __str__(self):
        return f"Collection: {self.name}"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)
