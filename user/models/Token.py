from django.db import models
from config.models import BaseModel

class Token(BaseModel):
    TOKEN_TYPE_CHOICES = [
        ("access", "Access"),
        ("refresh", "Refresh"),
    ]

    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="tokens")
    device = models.ForeignKey("user.Device", on_delete=models.CASCADE, related_name="device_tokens",null=True, blank=True)

    token_type = models.CharField(max_length=20, choices=TOKEN_TYPE_CHOICES)
    token_value = models.TextField()  # store hashed token (never plain)
    expired_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "token"
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["token_type"]),
            models.Index(fields=["expired_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Token({self.token_type}) for {getattr(self.user, 'email', str(self.user))}"
