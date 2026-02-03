from django.utils import timezone
from django.db import models
from config.models import BaseModel

from django.contrib.auth import get_user_model
User = get_user_model()


class Image(BaseModel):
    """
    Images: quản lý ảnh hệ thống.
    - type: phân loại để dễ filter (avatar/cover/word/theme/other)
    - associated: generic relation (content_type + object_id) để liên kết với bất kỳ entity nào
    """
    TYPE_CHOICES = [
        ("avatar", "Avatar"),
        ("cover", "Cover"),
        ("word", "Word"),
        ("collection", "Collection"),
        ("theme", "Theme"),
        ("other", "Other"),
    ]

    url = models.URLField(max_length=1024)

    # associated_type = models.CharField(max_length=50, choices=TYPE_CHOICES, default="other")
    # associated_id = models.CharField(max_length=255, null=True, blank=True)
    
    uploader = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_images")
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "image"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["type"]),
            models.Index(fields=["uploader"])
        ]

    def __str__(self):
        short = (self.url[:60] + "...") if len(self.url) > 63 else self.url
        return f"Image({self.type}) {short}"