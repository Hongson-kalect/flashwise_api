from django.db import models
from config.models import BaseModel 

class ModifierLog(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="modifier_logs")
    method = models.CharField(max_length=100, blank=True)
    target_type = models.CharField(max_length=100, blank=True)  # e.g. "lesson", "profile", "word",...
    target_id = models.CharField(max_length=100, null=True, blank=True) # /:id parameter
    path = models.CharField(max_length=512, blank=True, null=True)
    
    ACTION_CHOICES = [
        ("post", "Post"),
        ("put", "Put"),
        ("patch", "Patch"),
        ("delete", "Delete"),
    ]
    action = models.CharField(max_length=100, choices=ACTION_CHOICES)
    meta = models.JSONField(default=dict, blank=True)  # any contextual data, data, if file: record url, result, full_url
    is_success = models.BooleanField(default=False)

    class Meta:
        db_table = "modifier_log"
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["action"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        t = f"{self.action}"
        if self.target_type:
            t += f" -> {self.target_type}"
        return f"Activity: {t} by {getattr(self.user, 'email', str(self.user))}"
