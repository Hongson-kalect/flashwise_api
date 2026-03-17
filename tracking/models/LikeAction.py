
from django.utils import timezone
from email.policy import default
from config.models import BaseModel
from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import ArrayField

class LikeAction(BaseModel):
    # Like action theo đối tượng của server
    # Dùng Generic Foreign Key để Like được cho cả Word, Sense, Example...
    type = models.CharField(max_length=50)
    target_object_id = models.UUIDField()

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="like_actions")
    is_like = models.BooleanField(default=True, null=True) # 1: like, -1: dislike, 0 = neutral

    class Meta:
        unique_together = ('user', 'type', 'target_object_id')
        indexes = [
            models.Index(fields=['user', 'type', 'target_object_id'], name='like_action_index')
        ]
