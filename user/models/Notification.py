from django.db import models
from config.models import BaseModel

class Notification(BaseModel):
    TYPE_CHOICES = [
        ('system', 'System'),
        ('daily', 'Daily'),
        ('progress', 'Progress'),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='notifications')
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    title = models.CharField(max_length=255)
    content = models.TextField(blank=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        db_table = 'notification'
        ordering = ['-created_at']
