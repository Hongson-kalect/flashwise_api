"""Hồ sơ của chính người dùng và cấu hình công khai cho app."""
import json

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from tracking.sync import clean_profile, refresh_stats
from user.deletion import delete_account, export_data
from user.models import Device, UserProfile
from user.serializers.Profile import ProfileSerializer
from utils.utils.permissions import ExportThrottle


@api_view(['GET', 'PATCH', 'DELETE'])
def me(request):
    """
    GET: hồ sơ. PATCH: chỉ đổi được thông tin cá nhân và ngôn ngữ; XP, streak, tier do server giữ.
    DELETE: xoá tài khoản ngay, không khôi phục được (user/deletion.py). Body {confirm: true} (thiếu: 400 confirm_required).
    """
    if request.method == 'DELETE':
        if request.data.get('confirm') is not True:
            return Response({'detail': 'Send {"confirm": true} to delete the account', 'errorCode': 'confirm_required'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'deleted': True, **delete_account(request.user)})
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
@throttle_classes([ExportThrottle])
def export(request):
    """Tải toàn bộ dữ liệu của mình dạng JSON (file đính kèm), mặc định tối đa 5 lần / ngày."""
    body = json.dumps(export_data(request.user), cls=DjangoJSONEncoder, ensure_ascii=False)
    response = HttpResponse(body, content_type='application/json; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="flashwise-export-{timezone.now():%Y%m%d}.json"'
    return response


@api_view(['POST'])
def device(request):
    """
    Cập nhật thiết bị đang dùng (thiết bị được tạo lúc đăng nhập). Body: device_id, push_token (rỗng = thôi nhận
    thông báo), os, app_version, device_name. Thiết bị chưa đăng nhập: 404.
    """
    row = Device.objects.filter(user=request.user, device_id=(request.data.get('device_id') or '').strip()).first()
    if row is None:
        return Response({'detail': 'Device not found', 'errorCode': 'device_not_found'}, status=status.HTTP_404_NOT_FOUND)
    if 'push_token' in request.data:
        row.push_token = request.data.get('push_token') or None
    for field, limit in (('os', 50), ('app_version', 50), ('device_name', 150)):
        if request.data.get(field):
            setattr(row, field, str(request.data[field])[:limit])
    row.save()
    return Response({'device_id': row.device_id, 'push_token': row.push_token, 'os': row.os, 'app_version': row.app_version})


@api_view(['GET'])
@permission_classes([AllowAny])
@authentication_classes([])
@throttle_classes([AnonRateThrottle])
def app_config(request):
    """App gọi lúc mở: dưới min_version thì bắt cập nhật."""
    return Response({'min_version': settings.APP_MIN_VERSION, 'latest_version': settings.APP_LATEST_VERSION})
