from config.models import BaseModel
from django.db import models


class Login(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='login_history')
    device = models.ForeignKey('user.Device', on_delete=models.SET_NULL, related_name='device_login_history', blank=True, null=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True) 
    login_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'login_his'
        ordering = ['-login_at']

    def __str__(self):
        return self.user