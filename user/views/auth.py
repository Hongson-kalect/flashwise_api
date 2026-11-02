"""
Đăng nhập theo thiết bị (guest) + refresh token xoay vòng.
Mỗi refresh token gắn với 1 UserSession qua jti -> có thể thu hồi từng phiên.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, authentication_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from uuid6 import uuid7

from user.models import Device, UserProfile
from user.models.UserSession import UserSession
from user.serializers.Profile import ProfileSerializer

User = get_user_model()


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _issue_tokens(request, user, device):
    """Tạo cặp token mới và 1 phiên mới cho thiết bị (phiên cũ của thiết bị bị vô hiệu)."""
    refresh = RefreshToken.for_user(user)
    UserSession.objects.filter(user=user, device=device, is_active=True).update(is_active=False)
    UserSession.objects.create(
        user=user,
        device=device,
        ip_address=_client_ip(request),
        client_name=request.META.get("HTTP_USER_AGENT", "")[:100],
        refresh_token_jti=refresh["jti"],
    )
    return {
        "accessToken": str(refresh.access_token),
        "refreshToken": str(refresh),
    }


def _token_response(tokens, user):
    profile = UserProfile.objects.filter(user=user).first()
    return Response({
        **tokens,
        "user_info": ProfileSerializer(profile).data if profile else None,
    })


def _device_info(data):
    return {
        "os": (data.get("os") or "unknown")[:50],
        "app_version": (data.get("app_version") or "")[:50],
        "device_name": data.get("device_name"),
        "push_token": data.get("push_token"),
    }


def _device_for(user, data):
    """Thiết bị (user, device_id) cho lần đăng nhập tài khoản thật; cập nhật thông tin gửi kèm. Trả (device, Response lỗi)."""
    device_id = (data.get("device_id") or "").strip()
    if not device_id or len(device_id) > 255:
        return None, Response({"detail": "Missing device_id", "errorCode": "device_id_missing"}, status=status.HTTP_400_BAD_REQUEST)
    info = _device_info(data)
    device = Device.objects.filter(user=user, device_id=device_id).first()
    if device is None:
        return Device.objects.create(user=user, device_id=device_id, **info), None
    for field, value in info.items():
        if value and (field != "os" or value != "unknown"):
            setattr(device, field, value)
    device.is_deleted = False
    device.save()
    return device, None


def _read_refresh_token(request):
    # App gửi camelCase (refreshToken), giữ thêm snake_case cho client khác
    return request.data.get("refreshToken") or request.data.get("refresh_token")


@api_view(["POST"])
@permission_classes([AllowAny])
@authentication_classes([])
@throttle_classes([AnonRateThrottle])
def device_login(request):
    """
    Body: device_id (bắt buộc, UUID app tự sinh và giữ trong SecureStore), os, app_version, device_name, push_token
    - Thiết bị mới: tạo tài khoản guest + device.
    - Thiết bị cũ của guest: cấp lại token.
    - Thiết bị cũ của tài khoản thật: bắt buộc dùng refresh token / đăng nhập lại.
    """
    device_id = (request.data.get("device_id") or "").strip()
    if not device_id or len(device_id) > 255:
        return Response({"detail": "Missing device_id", "errorCode": "device_id_missing"}, status=status.HTTP_400_BAD_REQUEST)

    device_info = _device_info(request.data)

    device = Device.objects.select_related("user").filter(device_id=device_id, is_deleted=False).order_by("-last_seen_at").first()

    if device is None:
        with transaction.atomic():
            user = User(username=f"guest_{uuid7().hex}")
            user.set_unusable_password()
            user.save()  # signal tự tạo UserProfile (tier mặc định: guest)
            device = Device.objects.create(user=user, device_id=device_id, **device_info)
        return _token_response(_issue_tokens(request, user, device), user)

    user = device.user
    profile = UserProfile.objects.filter(user=user).first()
    # Bị ban (profile.is_active=False) vẫn đăng nhập để học tiếp, chỉ bị chặn việc công khai (NotBanned)
    if not user.is_active:
        return Response({"detail": "Account disabled", "errorCode": "account_disabled"}, status=status.HTTP_403_FORBIDDEN)

    if profile is None or profile.tier != "guest":
        return Response({"detail": "Login required", "errorCode": "login_required"}, status=status.HTTP_401_UNAUTHORIZED)

    for field, value in device_info.items():
        if value:
            setattr(device, field, value)
    device.save()
    return _token_response(_issue_tokens(request, user, device), user)


@api_view(["POST"])
@permission_classes([AllowAny])
@authentication_classes([])
@throttle_classes([AnonRateThrottle])
def refresh_token(request):
    """Body: refreshToken. Trả cặp token mới, token cũ không dùng lại được."""
    raw = _read_refresh_token(request)
    try:
        refresh = RefreshToken(raw)
    except TokenError:
        return Response({"detail": "Token invalid. Please re-authenticate.", "errorCode": "token_invalid"}, status=status.HTTP_401_UNAUTHORIZED)

    session = UserSession.objects.select_related("user", "device").filter(refresh_token_jti=refresh["jti"]).first()
    if session is None or not session.is_active or not session.user.is_active:
        if session is not None:
            # Token đã bị xoay/thu hồi mà vẫn bị dùng lại -> nghi bị lộ, đóng mọi phiên của thiết bị
            UserSession.objects.filter(user=session.user, device=session.device).update(is_active=False)
        return Response({"detail": "Token revoked. Please re-authenticate.", "errorCode": "token_revoked"}, status=status.HTTP_401_UNAUTHORIZED)

    return Response(_issue_tokens(request, session.user, session.device))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    """
    Body: refreshToken (không bắt buộc). Có token thì đóng phiên đó, không có thì đóng mọi phiên của user.
    Thiết bị của phiên bị đóng thôi nhận thông báo đẩy (xoá push_token).
    """
    raw = _read_refresh_token(request)
    sessions = UserSession.objects.filter(user=request.user, is_active=True)
    if raw:
        try:
            sessions = sessions.filter(refresh_token_jti=RefreshToken(raw)["jti"])
        except TokenError:
            pass
    device_ids = list(sessions.values_list("device_id", flat=True))
    sessions.update(is_active=False)
    Device.objects.filter(pk__in=device_ids).update(push_token=None)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([AllowAny])
@authentication_classes([])
def dev_token(request):
    """Chỉ dùng khi DEBUG: lấy token cho 1 user để test (?username=...). Không được đăng ký route khi DEBUG=False."""
    if not settings.DEBUG:
        return Response(status=status.HTTP_404_NOT_FOUND)
    username = request.query_params.get("username")
    user = User.objects.filter(username=username).first() if username else User.objects.filter(is_superuser=True).first()
    if user is None:
        return Response({"detail": "User not found"}, status=status.HTTP_404_NOT_FOUND)
    refresh = RefreshToken.for_user(user)
    return Response({"accessToken": str(refresh.access_token), "refreshToken": str(refresh)})
