from django.db import models
from config.models import BaseModel

class CommonPhrase(BaseModel):
    sub_id = models.CharField(max_length=100, blank=True, null=True, db_index=True) # uuid v7
    phrase = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    image = models.FileField(upload_to='images/common_phrases', null=True, blank=True)
    # words = models.ManyToManyField('Word', blank=True) nên tạo FK ở word thay vì ở Phrase
    # image_url = models.URLField(blank=True, null=True) Đã dùng bảng image liên kết với bảng, chủ động add image khi post
    score = models.IntegerField(default=100, null=True)
    tag = models.ManyToManyField('utils.Tag', blank=True)
    language_code = models.CharField(max_length=10, blank=True, null=True)

    class Meta:
        db_table = 'common phrase'
        ordering = ['-created_at']
        indexes  = [
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f"Phrase: {self.name}"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)
