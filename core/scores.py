"""
Điểm xếp hạng bộ từ (Collection.score, thứ tự mặc định của danh sách bộ và gợi ý). Chủ yếu theo chất lượng của chính bộ,
mọi tín hiệu tăng theo log nên vài bộ cực hot không đè hết:
- thích (100), tải về (60), được remix (80: có người thấy đáng sửa lại để dùng), xem (10, tín hiệu yếu vì dễ tăng).
- bộ chính thức (admin duyệt) +300; bộ quá nhỏ (< 5 nghĩa) trừ 50 vì ít giá trị học.
- uy tín tác giả chỉ cộng / trừ rất nhẹ (-5..+10, bằng chưa tới 1 lượt thích đầu tiên).
Điểm được tính lại khi có tín hiệu mới (thích, tải, remix, phát hành); xem (view_count) tăng liên tục nên chỉ được tính vào
lần tính tiếp theo, không tính lại mỗi lượt xem.
"""
from django.db.models import Case, F, FloatField, IntegerField, OuterRef, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce, Greatest, Least, Ln

from core.models import Collection

LIKE_WEIGHT = 100
DOWNLOAD_WEIGHT = 60
REMIX_WEIGHT = 80
VIEW_WEIGHT = 10
OFFICIAL_BONUS = 300
SMALL_SET_SIZE, SMALL_SET_PENALTY = 5, 50
AUTHOR_MIN, AUTHOR_MAX = -50, 100 # Kẹp uy tín tác giả rồi chia 10


def log_term(field, weight):
    return Value(float(weight)) * Ln(Greatest(F(field), Value(0)) + Value(1.0))


def score_expression():
    from user.models import UserProfile

    reputation = Coalesce(
        Subquery(UserProfile.objects.filter(user_id=OuterRef('created_by_id')).values('reputation')[:1]), Value(0),
    )
    author = Least(Greatest(reputation, Value(AUTHOR_MIN)), Value(AUTHOR_MAX)) / Value(10.0)
    quality = (
        log_term('like_count', LIKE_WEIGHT) + log_term('downloaded_count', DOWNLOAD_WEIGHT)
        + log_term('remix_count', REMIX_WEIGHT) + log_term('view_count', VIEW_WEIGHT)
    )
    official = Case(When(is_official=True, then=Value(float(OFFICIAL_BONUS))), default=Value(0.0), output_field=FloatField())
    small = Case(When(item_count__lt=SMALL_SET_SIZE, then=Value(float(-SMALL_SET_PENALTY))), default=Value(0.0), output_field=FloatField())
    return Cast(Cast(quality + author + official + small, FloatField()), IntegerField())


def refresh_collection_scores(**lookup):
    """Tính lại score cho các bộ khớp lookup (vd pk=..., created_by=...)."""
    return Collection.objects.filter(**lookup).update(score=score_expression())
