from config.models import BaseModel
from django.db import models

class UpdateLog(BaseModel):
    type = models.CharField(
        max_length=20,
        choices=[("word", "Word"), ("translate", "Translate")],
    )
    target_id = models.CharField(max_length=50)
    value = models.JSONField(default=dict,blank=True)

    request_by = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="update_request_logs", null=True, blank=True)
    change_from = models.JSONField(default=dict,blank=True)
    change_to = models.JSONField(default=dict,blank=True)

    def __str__(self):
        return f"RequestChange({self.type}:{self.target_sub_id})"