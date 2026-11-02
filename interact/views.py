"""
Người dùng báo lỗi nội dung / góp ý; admin duyệt report theo hàng ưu tiên (trọng số = độ tin cậy người report),
xử lý report (cộng / trừ uy tín người report) và ban / gỡ ban (interact/moderation.py).
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Max, Min, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from ai.models import AISense, AIWord
from ai.queries import visible_senses
from ai.validators import is_uuid
from core.models import Collection
from core.queries import visible_collections
from interact.moderation import TARGETS as BAN_TARGETS, ModerationError, ban, unban
from interact.models import Feedback, ModerationAction, Report
from user.models import UserProfile
from user.reputation import OUTCOME_KINDS, record, user_trust
from utils.utils.permissions import FeedbackThrottle, NotBanned

TYPES = {c[0] for c in Feedback.TYPE_CHOICES}
TARGETS = {c[0] for c in Report.TARGET_CHOICES}
OUTCOMES = {c[0] for c in Report.OUTCOME_CHOICES}
QUEUE_LIMIT = 100
QUEUE_REPORTS_PER_TARGET = 10


def _bad(message):
    return Response({'detail': message}, status=status.HTTP_400_BAD_REQUEST)


def _report_target_exists(user, target_type, target_id):
    if target_type == 'sense':
        return visible_senses(user).filter(pk=target_id).exists()
    if target_type == 'collection':
        return visible_collections(user).filter(pk=target_id).exists()
    if target_type == 'word':
        return AIWord.objects.filter(pk=target_id).exclude(is_deleted=True).exists()
    if target_type == 'user':
        return UserProfile.objects.filter(pk=target_id).exists()
    return True  # quiz: chưa có API quiz, giữ như cũ


@api_view(['POST'])
@permission_classes([IsAuthenticated, NotBanned])
@throttle_classes([FeedbackThrottle])
def create_report(request):
    """
    Body: target_type (word / sense / collection / user / quiz), target_id (user: id hồ sơ), reason (≤255, bắt buộc), type, message.
    Đối tượng phải tồn tại và người gửi nhìn thấy được. Mỗi người chỉ có 1 report đang chờ cho 1 đối tượng: gửi lại thì cập nhật
    report đó (200) thay vì tạo mới, nên spam report không đẩy đối tượng lên đầu hàng duyệt. Trọng số = độ tin cậy người gửi.
    """
    data = request.data
    reason = data.get('reason')
    target_type, target_id = data.get('target_type'), data.get('target_id')
    if target_type not in TARGETS or not is_uuid(target_id):
        return _bad('target_type hoặc target_id không hợp lệ')
    if not isinstance(reason, str) or not reason.strip():
        return _bad('reason là bắt buộc')
    kind = data.get('type', 'content')
    if kind not in TYPES:
        return _bad('type không hợp lệ')
    if not _report_target_exists(request.user, target_type, target_id):
        return Response({'detail': 'Không tìm thấy đối tượng'}, status=status.HTTP_404_NOT_FOUND)
    message = data.get('message')
    fields = {'type': kind, 'reason': reason.strip()[:255], 'message': message.strip()[:5000] if isinstance(message, str) else None}
    with transaction.atomic():
        report = Report.objects.select_for_update().filter(
            user=request.user, target_type=target_type, target_id=target_id, status='pending',
        ).first()
        if report is not None:
            Report.objects.filter(pk=report.pk).update(**fields, updated_at=timezone.now())
            return Response({'id': report.pk, 'status': report.status, 'updated': True}, status=status.HTTP_200_OK)
        report = Report.objects.create(
            user=request.user, created_by=request.user, target_type=target_type, target_id=target_id,
            weight=round(user_trust(request.user), 4), **fields,
        )
    return Response({'id': report.pk, 'status': report.status}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@throttle_classes([FeedbackThrottle])
def create_feedback(request):
    """Body: message (bắt buộc, ≤5000), type, target_id (không bắt buộc). User bị ban vẫn gửi được (để khiếu nại)."""
    data = request.data
    message = data.get('message')
    if not isinstance(message, str) or not message.strip():
        return _bad('message là bắt buộc')
    kind = data.get('type', 'feature')
    if kind not in TYPES:
        return _bad('type không hợp lệ')
    target_id = data.get('target_id')
    if target_id is not None and not is_uuid(target_id):
        return _bad('target_id không hợp lệ')
    feedback = Feedback.objects.create(
        user=request.user, created_by=request.user, type=kind, message=message.strip()[:5000], target_id=target_id,
    )
    return Response({'id': feedback.pk, 'status': feedback.status}, status=status.HTTP_201_CREATED)


def _previews(groups):
    """Thông tin ngắn của từng đối tượng trong hàng duyệt: {(target_type, id): {...}}."""
    ids = {}
    for group in groups:
        ids.setdefault(group['target_type'], []).append(group['target_id'])
    out = {}
    for word in AIWord.objects.filter(pk__in=ids.get('word', [])):
        out[('word', word.pk)] = {'value': word.value, 'language_code': word.language_code, 'status': word.status}
    for sense in AISense.objects.filter(pk__in=ids.get('sense', [])).select_related('created_by'):
        out[('sense', sense.pk)] = {
            'word_value': sense.word_value, 'word_id': str(sense.word_id), 'language_code': sense.language_code,
            'is_official': sense.is_official, 'is_active': sense.is_active is not False,
            'author': sense.created_by.username if sense.created_by_id else None, 'contents': sense.contents, 'delta': sense.delta,
        }
    for collection in Collection.objects.filter(pk__in=ids.get('collection', [])).select_related('created_by'):
        out[('collection', collection.pk)] = {
            'name': collection.name, 'description': collection.description, 'is_official': collection.is_official,
            'is_active': collection.is_active is not False, 'author': collection.created_by.username if collection.created_by_id else None,
        }
    for profile in UserProfile.objects.filter(pk__in=ids.get('user', [])).select_related('user'):
        out[('user', profile.pk)] = {
            'username': profile.user.username, 'full_name': profile.full_name, 'banned': profile.is_active is False,
            'reputation': profile.reputation,
        }
    return out


@api_view(['GET'])
@permission_classes([IsAdminUser])
def report_queue(request):
    """
    Admin: hàng report đang chờ, gộp theo đối tượng, đối tượng có tổng trọng số cao (nhiều người đáng tin cùng báo) lên trước.
    Query: target_type, limit (≤100). Mỗi đối tượng kèm tối đa 10 report mới nhất và thông tin ngắn của đối tượng.
    """
    params = request.query_params
    rows = Report.objects.filter(status='pending')
    if params.get('target_type'):
        rows = rows.filter(target_type=params['target_type'])
    try:
        limit = min(max(int(params.get('limit', 50)), 1), QUEUE_LIMIT)
    except ValueError:
        return _bad('limit không hợp lệ')
    groups = list(
        rows.values('target_type', 'target_id')
        .annotate(count=Count('id'), weight=Sum('weight'), first_at=Min('created_at'), last_at=Max('created_at'))
        .order_by('-weight', 'first_at')[:limit]
    )
    previews = _previews(groups)
    result = []
    for group in groups:
        reports = rows.filter(target_type=group['target_type'], target_id=group['target_id']).select_related('user')
        result.append({
            **group, 'weight': round(group['weight'] or 0, 4), 'target': previews.get((group['target_type'], group['target_id'])),
            'reports': [
                {'id': r.pk, 'user': r.user.username, 'type': r.type, 'reason': r.reason, 'message': r.message,
                 'weight': r.weight, 'created_at': r.created_at}
                for r in reports.order_by('-created_at')[:QUEUE_REPORTS_PER_TARGET]
            ],
        })
    return Response({'targets': result})


def _parse_expiry(data):
    """days (số ngày) hoặc expires_at (ISO); không gửi = vĩnh viễn. Trả (thời điểm, lỗi)."""
    if data.get('days') not in (None, ''):
        try:
            days = float(data['days'])
        except (TypeError, ValueError):
            return None, 'days không hợp lệ'
        return (timezone.now() + timedelta(days=days), None) if days > 0 else (None, 'days phải > 0')
    raw = data.get('expires_at')
    if raw in (None, ''):
        return None, None
    value = parse_datetime(raw) if isinstance(raw, str) else None
    if value is None:
        return None, 'expires_at không hợp lệ'
    return (value if timezone.is_aware(value) else timezone.make_aware(value)), None


def _moderation_error(error):
    code = str(error)
    return Response({'detail': code, 'errorCode': code}, status=status.HTTP_404_NOT_FOUND if code == 'not_found' else status.HTTP_400_BAD_REQUEST)


def _ban_target(request, data, report=None):
    """Ban theo body; trả (ModerationAction, Response lỗi)."""
    target_type, target_id = data.get('target_type'), data.get('target_id')
    if target_type not in BAN_TARGETS or not is_uuid(target_id):
        return None, _bad('target_type (user / collection / word / sense) hoặc target_id không hợp lệ')
    expires_at, error = _parse_expiry(data)
    if error:
        return None, _bad(error)
    reason = data.get('reason') if isinstance(data.get('reason'), str) else ''
    try:
        action = ban(request.user, target_type, target_id, reason=reason[:2000], expires_at=expires_at,
                     hide_content=bool(data.get('hide_content')), report=report)
    except ModerationError as e:
        return None, _moderation_error(e)
    return action, None


def _action_data(action):
    return {
        'id': action.pk, 'target_type': action.target_type, 'target_id': str(action.target_id), 'action': action.action,
        'reason': action.reason, 'expires_at': action.expires_at, 'affected': action.affected,
        'by': action.created_by.username if action.created_by_id else None, 'created_at': action.created_at,
    }


@api_view(['POST'])
@permission_classes([IsAdminUser])
def resolve_reports(request):
    """
    Admin: xử lý mọi report đang chờ của 1 đối tượng. Body: target_type, target_id, outcome (accepted / duplicate / rejected /
    spam), result (phản hồi), report_ids (tuỳ chọn: chỉ xử lý các report này), ban ({reason, days | expires_at, hide_content}:
    ban luôn đối tượng, chỉ khi outcome=accepted). Mỗi người report được cộng / trừ uy tín theo outcome.
    """
    data = request.data
    target_type, target_id, outcome = data.get('target_type'), data.get('target_id'), data.get('outcome')
    if target_type not in TARGETS or not is_uuid(target_id) or outcome not in OUTCOMES:
        return _bad('target_type, target_id, outcome không hợp lệ')
    result = data.get('result') if isinstance(data.get('result'), str) else None
    ban_data = data.get('ban')
    if ban_data is not None and (not isinstance(ban_data, dict) or outcome != 'accepted'):
        return _bad('ban phải là object và chỉ dùng với outcome=accepted')
    report_ids = data.get('report_ids')
    if report_ids is not None and (not isinstance(report_ids, list) or not all(is_uuid(i) for i in report_ids)):
        return _bad('report_ids không hợp lệ')

    with transaction.atomic():
        rows = Report.objects.select_for_update().filter(status='pending', target_type=target_type, target_id=target_id)
        if report_ids is not None:
            rows = rows.filter(pk__in=report_ids)
        reports = list(rows)
        if not reports:
            return Response({'detail': 'Không có report đang chờ'}, status=status.HTTP_404_NOT_FOUND)
        action = None
        if ban_data is not None:
            action, error = _ban_target(request, {**ban_data, 'target_type': target_type, 'target_id': target_id}, report=reports[0])
            if error:
                return error
        now = timezone.now()
        for report in reports:
            report.status = 'resolved' if outcome in ('accepted', 'duplicate') else 'ignored'
            report.outcome, report.resolved_by, report.resolved_at = outcome, request.user, now
            if result is not None:
                report.result = result[:5000]
            report.save(update_fields=['status', 'outcome', 'resolved_by', 'resolved_at', 'result', 'updated_at'])
            if outcome in OUTCOME_KINDS:
                record(report.user, OUTCOME_KINDS[outcome], target_type, report.target_id, report=report, by=request.user)
    return Response({'resolved': len(reports), 'outcome': outcome, 'ban': _action_data(action) if action else None})


@api_view(['POST'])
@permission_classes([IsAdminUser])
def moderation_ban(request):
    """
    Admin: ban. Body: target_type (user / collection / word / sense), target_id (user: id hồ sơ), reason,
    days | expires_at (chỉ user, không gửi = vĩnh viễn), hide_content (chỉ user: ẩn luôn bộ / nghĩa đã công khai của họ).
    """
    action, error = _ban_target(request, request.data)
    return error or Response(_action_data(action), status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def moderation_unban(request):
    """Admin: gỡ ban, khôi phục trạng thái trước khi ban và hoàn lại điểm uy tín đã trừ. Body: target_type, target_id, reason."""
    data = request.data
    if data.get('target_type') not in BAN_TARGETS or not is_uuid(data.get('target_id')):
        return _bad('target_type hoặc target_id không hợp lệ')
    reason = data.get('reason') if isinstance(data.get('reason'), str) else ''
    try:
        action = unban(request.user, data['target_type'], data['target_id'], reason=reason[:2000])
    except ModerationError as e:
        return _moderation_error(e)
    return Response(_action_data(action), status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def moderation_history(request):
    """Admin: lịch sử ban / gỡ ban, mới nhất trước. Query: target_type, target_id (tuỳ chọn), tối đa 200 dòng."""
    rows = ModerationAction.objects.select_related('created_by')
    params = request.query_params
    if params.get('target_type'):
        rows = rows.filter(target_type=params['target_type'])
    if params.get('target_id'):
        if not is_uuid(params['target_id']):
            return _bad('target_id không hợp lệ')
        rows = rows.filter(target_id=params['target_id'])
    return Response({'actions': [_action_data(a) for a in rows[:200]]})
