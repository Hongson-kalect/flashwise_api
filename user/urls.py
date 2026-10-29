from django.conf import settings
from django.urls import path

from user.views import device_login, refresh_token, logout, dev_token
from user.views.me import me, app_config

urlpatterns = [
    path('auth/device-login/', device_login),
    path('auth/logout/', logout),
    path('me/', me),
    path('app-config/', app_config),
    path('refresh-token', refresh_token), # App gọi `${baseURL}/refresh-token` (không có / cuối)
    path('refresh-token/', refresh_token),
]

if settings.DEBUG:
    urlpatterns += [path('auth/dev-token/', dev_token)]
