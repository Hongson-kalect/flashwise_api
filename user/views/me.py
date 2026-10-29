"""Hồ sơ của chính người dùng và cấu hình công khai cho app."""
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from tracking.sync import clean_profile, refresh_stats
from user.models import UserProfile
from user.serializers.Profile import ProfileSerializer


@api_view(['GET', 'PATCH'])
def me(request):
    """GET: hồ sơ. PATCH: chỉ đổi được thông tin cá nhân và ngôn ngữ; XP, streak, tier do server giữ."""
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'PATCH':
        clean, errors = clean_profile(request.data)
        if errors:
            return Response({'detail': 'Dữ liệu không hợp lệ', 'errors': errors}, status=status.HTTP_400_BAD_REQUEST)
        for name, value in clean.items():
            setattr(profile, name, value)
        profile.save()
    else:
        refresh_stats(request.user)  # streak tính theo ngày nên có thể đã đổi từ lần sync trước
        profile.refresh_from_db()
    return Response(ProfileSerializer(profile).data)


@api_view(['GET'])
@permission_classes([AllowAny])
@authentication_classes([])
@throttle_classes([AnonRateThrottle])
def app_config(request):
    """App gọi lúc mở: dưới min_version thì bắt cập nhật."""
    return Response({'min_version': settings.APP_MIN_VERSION, 'latest_version': settings.APP_LATEST_VERSION})
