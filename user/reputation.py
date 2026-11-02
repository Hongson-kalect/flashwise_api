"""
Uy tín người dùng (ẩn, user không thấy con số). Hai chỉ số tách riêng:
- report_trust: độ tin cậy khi report = (đúng + 2) / (đúng + sai + 4). Người mới 0,5; vài report đầu không làm điểm nhảy mạnh.
  Dùng làm trọng số report để xếp hàng duyệt (tác dụng chính của uy tín).
- reputation: điểm đóng góp (nghĩa được chọn làm chính thức, report đúng; bị ban thì trừ). Chỉ cộng rất nhẹ vào điểm
  xếp hạng bộ từ của tác giả (core/scores.py), xếp hạng vẫn chủ yếu theo chất lượng của chính bộ.
Điểm trên profile luôn tính lại từ ReputationEvent theo bảng dưới, đổi bảng thì gọi refresh_reputation là áp cho cả lịch sử.
"""
from django.db.models import Count, Sum

from user.models import ReputationEvent, UserProfile

POINTS = {
    'report_accepted': 2,
    'report_rejected': 0, # Bác thì chỉ hạ độ tin cậy, không trừ điểm: tránh làm user ngại report
    'report_spam': -5,
    'sense_promoted': 20,
    'content_banned': -10,
    'content_unbanned': 10,
    'user_banned': -50,
    'user_unbanned': 50,
    'manual': 0, # Admin cộng / trừ tay, số điểm nằm ở cột points
}
# Report spam tính như 2 lần bị bác
REPORT_COUNTS = {'report_accepted': (1, 0), 'report_rejected': (0, 1), 'report_spam': (0, 2)}
OUTCOME_KINDS = {'accepted': 'report_accepted', 'rejected': 'report_rejected', 'spam': 'report_spam'}
TRUST_PRIOR_GOOD, TRUST_PRIOR_TOTAL = 2, 4


def report_trust(accepted, rejected):
    return (accepted + TRUST_PRIOR_GOOD) / (accepted + rejected + TRUST_PRIOR_TOTAL)


def user_trust(user):
    row = UserProfile.objects.filter(user=user).values('report_accepted', 'report_rejected').first()
    return report_trust(row['report_accepted'], row['report_rejected']) if row else report_trust(0, 0)


def refresh_reputation(user):
    """Tính lại điểm của user từ nhật ký rồi cập nhật điểm xếp hạng các bộ của họ."""
    from core.scores import refresh_collection_scores

    reputation = accepted = rejected = 0
    for row in ReputationEvent.objects.filter(user=user).values('kind').annotate(n=Count('id'), manual=Sum('points')):
        kind, n = row['kind'], row['n']
        reputation += (row['manual'] or 0) if kind == 'manual' else POINTS.get(kind, 0) * n
        good, bad = REPORT_COUNTS.get(kind, (0, 0))
        accepted, rejected = accepted + good * n, rejected + bad * n
    profile, _ = UserProfile.objects.get_or_create(user=user)
    UserProfile.objects.filter(pk=profile.pk).update(reputation=reputation, report_accepted=accepted, report_rejected=rejected)
    refresh_collection_scores(created_by=user)
    return reputation


def record(user, kind, target_type=None, target_id=None, report=None, points=0, note='', by=None):
    """Ghi 1 sự kiện uy tín rồi tính lại. User hệ thống / admin vẫn ghi bình thường (không ảnh hưởng gì)."""
    if user is None or kind not in POINTS:
        return None
    ReputationEvent.objects.create(
        user=user, kind=kind, points=points if kind == 'manual' else 0, target_type=target_type, target_id=target_id,
        report=report, note=note[:255], created_by=by,
    )
    return refresh_reputation(user)
