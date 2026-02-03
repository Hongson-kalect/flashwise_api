from django.db import models
from config.models import BaseModel

class Setting(BaseModel):
    user = models.OneToOneField(
        'auth.User',
        on_delete=models.CASCADE,
        related_name="setting",
        db_index=True,
    )
    theme = models.CharField(
        max_length=50,
        choices=[
            ("light", "Light"),
            ("dark", "Dark"),
            ("system", "System Default"),
        ],
        default="system",
    )
    app_language = models.ForeignKey('core.Language', on_delete=models.SET_NULL, null=True, blank=True, related_name='app_language_settings')
    language = models.ForeignKey('core.Language', on_delete=models.SET_NULL, null=True, blank=True, related_name='native_language_settings')
    learning_language = models.ForeignKey('core.Language', on_delete=models.SET_NULL, null=True, blank=True, related_name='learning_language_settings')
    notification_enabled = models.BooleanField(default=True)
    vibration = models.BooleanField(default=True)
    sound = models.JSONField(default=dict, blank=True)
    preferred_learning_mode = models.CharField(
        max_length=50,
        choices=[
            ("solo", "Solo"),
            ("group", "Group"),
            ("mixed", "Mixed"),
        ],
        blank=True,
    )
    preferred_learning_time = models.TimeField(
        null=True,
        blank=True,
    )
    target_type = models.CharField(
        max_length=50,
        choices=[
            ("daily", "Daily"),
            ("weekly", "Weekly"),
            ("monthly", "Monthly"),
        ],
        blank=True,
    )
    target_num = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "setting"
        indexes = [
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"Setting of {self.user.email}"
