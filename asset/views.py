"""Tải ảnh / audio lên (asset/media.py). App giữ `key` để gửi kèm khi sửa nghĩa / phát hành bộ, hiển thị bằng `url`."""
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ai.validators import is_uuid
from asset.media import MediaError, delete_media, save_upload
from asset.models import MediaFile
from core.storages import media_url
from utils.utils.permissions import NotBanned, consume_daily_quota


def media_row(media):
    return {
        'id': str(media.id), 'kind': media.kind, 'key': media.key, 'url': media_url(media.key),
        'content_type': media.content_type, 'size': media.size, 'width': media.width, 'height': media.height,
        'created_at': media.created_at,
    }


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, NotBanned])
@parser_classes([MultiPartParser, FormParser])
def media(request):
    """
    POST (multipart): file, kind = image | audio. Ảnh tối đa MEDIA_IMAGE_MAX_BYTES (5MB), thu nhỏ còn cạnh dài 1280 và đổi WebP;
    audio tối đa MEDIA_AUDIO_MAX_BYTES (2MB): mp3 / m4a / aac / ogg / wav / webm / caf. Mỗi user MEDIA_UPLOADS_PER_DAY file / ngày.
    Lỗi: 400 invalid_kind / invalid_file / file_missing, 413 file_too_large, 429 quota_exceeded.
    GET: file của mình, mới nhất trước (?kind).
    """
    if request.method == 'GET':
        rows = MediaFile.objects.filter(created_by=request.user).exclude(is_deleted=True).order_by('-created_at')
        if request.query_params.get('kind'):
            rows = rows.filter(kind=request.query_params['kind'])
        return Response([media_row(m) for m in rows[:200]])

    upload = request.FILES.get('file')
    if upload is None:
        return Response({'detail': 'file is required', 'errorCode': 'file_missing'}, status=status.HTTP_400_BAD_REQUEST)
    if not consume_daily_quota(request.user, 'media', 1, settings.MEDIA_UPLOADS_PER_DAY):
        return Response({'detail': 'Daily upload limit reached', 'errorCode': 'quota_exceeded'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    try:
        row = save_upload(request.user, request.data.get('kind'), upload)
    except MediaError as e:
        http = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if e.code == 'file_too_large' else status.HTTP_400_BAD_REQUEST
        return Response({'detail': str(e), 'errorCode': e.code}, status=http)
    return Response(media_row(row), status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def media_detail(request, pk):
    """Xoá file của mình (admin: bất kỳ). Nội dung đang trỏ tới khoá này sẽ mất ảnh / audio."""
    rows = MediaFile.objects.filter(pk=pk) if is_uuid(pk) else MediaFile.objects.none()
    if not request.user.is_staff:
        rows = rows.filter(created_by=request.user)
    row = rows.first()
    if row is None:
        return Response(status=status.HTTP_404_NOT_FOUND)
    delete_media(row)
    return Response(status=status.HTTP_204_NO_CONTENT)
