from django.db import models
from config.models.BaseModel import BaseModel


class Profile(BaseModel):
    user = models.OneToOneField(
        'auth.User',
        on_delete=models.CASCADE,
        related_name="profile",
        db_index=True,
    )
    full_name = models.CharField(max_length=255, blank=True)
    avatar_url = models.URLField(max_length=512, blank=True)
    age = models.PositiveIntegerField(null=True, blank=True)
    gender = models.CharField(
        max_length=20,
        choices=[
            ("male", "Male"),
            ("female", "Female"),
            ("other", "Other"),
        ],
        blank=True,
    )
    native_language = models.CharField(max_length=50, blank=True)
    dob = models.DateField(null=True, blank=True)
    country = models.CharField(max_length=100, blank=True)
    learning_languages = models.JSONField(default=list, blank=True)
    join_date = models.DateTimeField(auto_now_add=True)
    login_id = models.CharField(max_length=255, blank=True, unique=True)
    time_zone = models.CharField(max_length=100, blank=True)
    zone_num = models.IntegerField(null=True, blank=True)
    is_guest = models.BooleanField(default=True)
    TIER_CHOICES = [
        ("admin", "Admin"),
        ("normal_user", "Normal User"),
        ("guest", "Guest"),
        ("VIP", "VIP"),
    ]
    tier = models.CharField(
        max_length=20,
        choices=TIER_CHOICES,
        default="guest",
    )
    tier_expired_at = models.DateTimeField(null=True, blank=True)

    providerList =[("google", "Google"),("facebook", "Facebook"), ("apple", "Apple"), ("github", "GitHub"), ("x", "X (Twitter)")]
    provider = models.CharField(max_length=20, choices=providerList, null=True)
    provider_user_id = models.CharField(max_length=255, null=True)
    fav_time = models.TimeField(null=True, blank=True) # giờ học, setting học quanh giờ này

    level = models.IntegerField(default=1)
    birth_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "profile"
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["country"]),
        ]

    def __str__(self):
        return f"{self.full_name or self.user.email}"
