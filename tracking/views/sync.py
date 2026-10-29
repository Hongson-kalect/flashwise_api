"""API đồng bộ app <-> server. Luật gộp từng bảng ở tracking/sync.py."""
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from tracking import sync
from user.serializers.Profile import ProfileSerializer
from user.models import UserProfile


def _bad(message, **extra):
    return Response({'detail': message, **extra}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
def sync_push(request):
    """
    Body: {"changes": {"user_sense_progress": [...], "review_log": [...], "day_learn_log": [...], "learn_session": [...],
                       "user_note": [...], "user_settings": [{"key", "value", "updated_at"}], "user_profile": {...}}}
    Mỗi bảng tối đa MAX_ITEMS dòng, bảng nào không có thì bỏ qua. Trả kết quả từng bảng {accepted, skipped, rejected[{id, reason}]};
    dòng bị từ chối không chặn các dòng khác. Gửi lại cùng dữ liệu là an toàn.
    """
    changes = request.data.get('changes')
    if not isinstance(changes, dict):
        return _bad('changes phải là object {tên bảng: danh sách dòng}')
    unknown = [name for name in changes if name not in sync.PUSH_HANDLERS and name != 'user_profile']
    if unknown:
        return _bad('Bảng không hỗ trợ', tables=unknown)
    for name, items in changes.items():
        if name == 'user_profile':
            continue
        if not isinstance(items, list):
            return _bad(f'{name} phải là danh sách')
        if len(items) > sync.MAX_ITEMS:
            return _bad(f'{name} tối đa {sync.MAX_ITEMS} dòng mỗi lần', table=name)

    results = {}
    with transaction.atomic():
        for name, handler in sync.PUSH_HANDLERS.items():
            if name in changes:
                results[name] = handler(request.user, changes[name])
        if 'user_profile' in changes:
            results['user_profile'] = sync.push_profile(request.user, changes['user_profile'])
        if 'day_learn_log' in changes:
            sync.refresh_stats(request.user)

    return Response({'server_time': timezone.now(), 'results': results})


@api_view(['GET'])
def sync_pull(request):
    """
    Query: tables=a,b (mặc định: tất cả trừ review_log), limit (mặc định 500, tối đa 1000),
           cursors={"tên bảng": "<con trỏ>"} (JSON; bảng không có con trỏ thì lấy từ đầu).
    Trả {"tables": {tên: {items, has_more, cursor}}, "user_profile": {...}, "server_time"}.
    Còn has_more thì gọi lại với cursor mới của bảng đó. Dòng đã xoá mềm vẫn được trả (is_deleted) để app xoá theo.
    """
    import json

    names = request.query_params.get('tables')
    if names:
        names = [n.strip() for n in names.split(',') if n.strip()]
        bad = [n for n in names if n not in sync.PULL_TABLES]
        if bad:
            return _bad('Bảng không hỗ trợ', tables=bad)
    else:
        names = [n for n in sync.PULL_TABLES if n != 'review_log']

    try:
        limit = max(1, min(int(request.query_params.get('limit', 500)), sync.MAX_PULL))
    except ValueError:
        return _bad('limit phải là số')
    try:
        cursors = json.loads(request.query_params.get('cursors') or '{}')
        if not isinstance(cursors, dict):
            raise ValueError
        parsed = {name: sync.parse_cursor(cursors.get(name)) for name in names}
    except ValueError:
        return _bad('cursors sai định dạng')

    # Lấy mốc trước khi đọc để dòng đổi giữa chừng không bị bỏ sót ở lần sau
    server_time = timezone.now()
    tables = {name: sync.pull_table(request.user, name, parsed[name], limit) for name in names}
    profile = UserProfile.objects.filter(user=request.user).first()
    return Response({
        'server_time': server_time,
        'tables': tables,
        'user_profile': ProfileSerializer(profile).data if profile else None,
    })
