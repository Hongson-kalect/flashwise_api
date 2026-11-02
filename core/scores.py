"""
Điểm xếp hạng bộ từ (Collection.score, thứ tự mặc định của danh sách bộ): chủ yếu theo chất lượng của chính bộ
(lượt thích, lượt tải, tăng theo log nên vài bộ cực hot không đè hết), uy tín tác giả chỉ cộng / trừ rất nhẹ (-5..+10,
bằng chưa tới 1 lượt thích đầu tiên).
"""
from django.db.models import F, FloatField, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Cast, Coalesce, Greatest, Least, Ln

from core.models import Collection

LIKE_WEIGHT = 100
DOWNLOAD_WEIGHT = 60
AUTHOR_MIN, AUTHOR_MAX = -50, 100 # Kẹp uy tín tác giả rồi chia 10


def score_expression():
    from user.models import UserProfile

    reputation = Coalesce(
        Subquery(UserProfile.objects.filter(user_id=OuterRef('created_by_id')).values('reputation')[:1]), Value(0),
    )
    author = Least(Greatest(reputation, Value(AUTHOR_MIN)), Value(AUTHOR_MAX)) / Value(10.0)
    quality = (
        Value(float(LIKE_WEIGHT)) * Ln(Greatest(F('like_count'), Value(0)) + Value(1.0))
        + Value(float(DOWNLOAD_WEIGHT)) * Ln(Greatest(F('downloaded_count'), Value(0)) + Value(1.0))
    )
    return Cast(Cast(quality + author, FloatField()), IntegerField())


def refresh_collection_scores(**lookup):
    """Tính lại score cho các bộ khớp lookup (vd pk=..., created_by=...)."""
    return Collection.objects.filter(**lookup).update(score=score_expression())
