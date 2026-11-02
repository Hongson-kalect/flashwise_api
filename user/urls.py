from django.conf import settings
from django.urls import path

from user.views import device_login, refresh_token, logout, dev_token
from user.views.account import (
    email_login, email_register, password_change, password_forgot, password_reset, providers, social_login, unlink_provider,
)
from user.views.me import app_config, device, export, me

urlpatterns = [
    path('auth/device-login/', device_login),
    path('auth/logout/', logout),
    path('auth/social-login/', social_login),
    path('auth/email/register/', email_register),
    path('auth/email/login/', email_login),
    path('auth/password/forgot/', password_forgot),
    path('auth/password/reset/', password_reset),
    path('auth/password/change/', password_change),
    path('auth/providers/', providers),
    path('auth/providers/<str:provider>/', unlink_provider),
    path('me/', me),
    path('me/export/', export),
    path('me/device/', device),
    path('app-config/', app_config),
    path('refresh-token', refresh_token), # App gọi `${baseURL}/refresh-token` (không có / cuối)
    path('refresh-token/', refresh_token),
]

if settings.DEBUG:
    urlpatterns += [path('auth/dev-token/', dev_token)]
