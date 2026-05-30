from django.utils import timezone
from config.models import BaseModel
from django.db import models
from django.contrib.postgres.fields import ArrayField

class LearnSession(BaseModel):
    TYPE_CHOICES = [
        ("learn", "Learn"),
        ("quicklearn", "Quick Learn"),
        ("recall", "Recall"),
        ("review", "Review"),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="learn_sessions")
    start_at = models.DateTimeField(default=timezone.now)
    end_at = models.DateTimeField(blank=True, null=True)
    senses = ArrayField(base_field=models.CharField(max_length=50), blank=True, default=list) #word subId or user Word id 
    sense_count = models.PositiveIntegerField(default=0)
    time = models.PositiveIntegerField(default=0)  # tổng thời gian (giây)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="learn")

    def __str__(self):
        return f"LearnSession({self.user_id}, {self.type}, {self.start_at:%Y-%m-%d})"
