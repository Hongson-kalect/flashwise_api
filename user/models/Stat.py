from django.db import models
from config.models import BaseModel

class Stat(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='stats')
    total_words_learned = models.PositiveIntegerField(default=0)
    total_sessions = models.PositiveIntegerField(default=0)
    total_app_time = models.IntegerField(default=0)  # có thể thay bằng IntegerField (giây)
    current_streak = models.PositiveIntegerField(default=0)
    max_streak = models.PositiveIntegerField(default=0)
    total_xp = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'stat'
        ordering = ['-updated_at']

    def __str__(self):
        return f"Stat for {self.user.username}"