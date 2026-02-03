from django.db import models
from config.models import BaseModel

class QueryLog(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name="query_logs")
    target_type = models.CharField(max_length=100, blank=True)  # e.g. "lesson", "profile", "word",...
    target_id = models.CharField(max_length=100, null=True, blank=True) # /:id parameter
    path = models.CharField(max_length=512, blank=True, null=True)

    meta = models.JSONField(default=dict, blank=True)  # any contextual query, if file: record url, full_url
    is_success = models.BooleanField(default=False)

    class Meta:
        db_table = "query_log"
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["target_type"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"query: {self.target_type} by {getattr(self.user, 'email', str(self.user))}"
