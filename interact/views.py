"""Người dùng báo lỗi nội dung / góp ý. Chỉ tạo, xử lý bên admin."""
from rest_framework import status
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response

from ai.validators import is_uuid
from interact.models import Feedback, Report
from utils.utils.permissions import FeedbackThrottle

TYPES = {c[0] for c in Feedback.TYPE_CHOICES}
TARGETS = {c[0] for c in Report.TARGET_CHOICES}


def _bad(message):
    return Response({'detail': message}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@throttle_classes([FeedbackThrottle])
def create_report(request):
    """Body: target_type (word / collection / quiz), target_id, reason (≤255, bắt buộc), type, message."""
    data = request.data
    reason = data.get('reason')
    if data.get('target_type') not in TARGETS or not is_uuid(data.get('target_id')):
        return _bad('target_type hoặc target_id không hợp lệ')
    if not isinstance(reason, str) or not reason.strip():
        return _bad('reason là bắt buộc')
    kind = data.get('type', 'content')
    if kind not in TYPES:
        return _bad('type không hợp lệ')
    message = data.get('message')
    report = Report.objects.create(
        user=request.user, created_by=request.user, target_type=data['target_type'], target_id=data['target_id'],
        type=kind, reason=reason.strip()[:255], message=message.strip()[:5000] if isinstance(message, str) else None,
    )
    return Response({'id': report.pk, 'status': report.status}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@throttle_classes([FeedbackThrottle])
def create_feedback(request):
    """Body: message (bắt buộc, ≤5000), type, target_id (không bắt buộc)."""
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
