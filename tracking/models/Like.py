
from django.utils import timezone
from email.policy import default
from config.models import BaseModel
from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import ArrayField


class LikeSummary(BaseModel):
    # Like action theo đối tượng của server
    # Dùng Generic Foreign Key để Like được cho cả Word, Sense, Example...
    type = models.CharField(max_length=50)
    target_id = models.UUIDField()
    
    like_count = models.IntegerField(default=0)
    dislike_count = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('type', 'target_id')
        indexes = [
            models.Index(fields=['target_id'], name='like_summary_index')
        ]
