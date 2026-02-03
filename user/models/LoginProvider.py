from django.db import models
from config.models import BaseModel

class LoginProvider(BaseModel):
    PROVIDER_CHOICES = [
        ('google', 'Google'),
        ('facebook', 'Facebook'),
        ('apple', 'Apple'),
        ('github', 'GitHub'),
        ('x', 'X (Twitter)'),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='login_providers')
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES)
    avatar_url = models.URLField(blank=True, null=True)
    url = models.URLField(blank=True, null=True)  # profile URL từ provider
    bio = models.TextField(blank=True)
    gender = models.CharField(max_length=20, blank=True, null=True)
    dob = models.DateField(blank=True, null=True)
    token = models.TextField(blank=True, null=True)  # access token (thường chỉ lưu tạm hoặc mã hóa)

    class Meta:
        db_table = 'login_provider'
        unique_together = ('user', 'provider')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.provider.capitalize()} login for {getattr(self.user, 'email', str(self.user))}"
