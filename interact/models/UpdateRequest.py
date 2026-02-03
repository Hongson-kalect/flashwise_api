from config.models import BaseModel
from django.db import models

class UpdateRequest(BaseModel):
    type = models.CharField(
        max_length=20,
        choices=[("word", "Word"), ("translate", "Translate")],
    )
    target_id = models.CharField(max_length=50)
    value = models.JSONField(default=dict,blank=True)

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="requests")
    updated_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name="requests_updated"
    )

    is_active = models.BooleanField(default=True)
    is_approval = models.BooleanField(default=False)
    approval_at = models.DateTimeField(null=True, blank=True)
    sync = models.BooleanField(default=False)

    def __str__(self):
        return f"RequestChange({self.type}:{self.target_sub_id})"