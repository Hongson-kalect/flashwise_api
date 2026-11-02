"""
Thích (like / tim) nghĩa và bộ từ: mỗi (user, đối tượng, version) 1 dòng UserInteraction, bật / tắt bằng status.
Cột like_count của đối tượng = số user đang thích ít nhất 1 version, nên 1 user thích nhiều version vẫn chỉ tính 1.
Nghĩa không có version (luôn 0): mỗi bản nghĩa (chính thức / tuỳ chỉnh) là 1 dòng AISense riêng nên đếm riêng.
"""
from django.db import transaction
from django.db.models import Count, F

from interact.models import UserInteraction


def like_rows(target_type, target_id, user=None):
    rows = UserInteraction.objects.filter(interaction_type='like', target_type=target_type, target_id=target_id)
    return rows.filter(user=user) if user is not None else rows


def liked_versions(user, target_type, target_id):
    if user is None or not user.is_authenticated:
        return []
    return sorted(like_rows(target_type, target_id, user).filter(status=True).values_list('version', flat=True))


def version_like_counts(target_type, target_id):
    """{version: số người đang thích version đó}."""
    rows = like_rows(target_type, target_id).filter(status=True).values('version').annotate(n=Count('id'))
    return {row['version']: row['n'] for row in rows}


def set_like(user, model, target_type, target_id, want, version=0):
    """
    POST (want=True): thích `version`. DELETE: bỏ thích `version`, version=None thì bỏ mọi version.
    Khoá dòng đối tượng nên 2 request đồng thời không cộng / trừ like_count 2 lần. Trả (liked_versions, like_count).
    """
    with transaction.atomic():
        model.objects.select_for_update().filter(pk=target_id).exists()
        mine = like_rows(target_type, target_id, user)
        before = mine.filter(status=True).exists()
        if want:
            UserInteraction.objects.update_or_create(
                user=user, interaction_type='like', target_type=target_type, target_id=target_id, version=version,
                defaults={'status': True, 'created_by': user},
            )
        else:
            (mine if version is None else mine.filter(version=version)).filter(status=True).update(status=False)
        after = mine.filter(status=True).exists()
        if after != before:
            model.objects.filter(pk=target_id).update(like_count=F('like_count') + (1 if after else -1))
        like_count = model.objects.filter(pk=target_id).values_list('like_count', flat=True).first() or 0
    return liked_versions(user, target_type, target_id), max(like_count, 0)
