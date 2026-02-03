from django.db import models
from config.models import BaseModel

class Device(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='devices')
    device_id = models.CharField(max_length=255)
    os = models.CharField(max_length=100)
    app_version = models.CharField(max_length=50)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'device'
        ordering = ['-last_seen_at']
