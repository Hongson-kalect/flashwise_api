"""
Điểm xếp hạng nghĩa (AISense.score): nghĩa nào được chọn khi thêm từ vào bộ ở chế độ nhanh (ai/bulk.py best_official_senses)
và thứ tự các nghĩa ở trang từ (ai/queries.py order_senses). Chỉ tổng hợp tín hiệu thụ động, không dựa vào like:
- admin / seeder đề xuất: rất mạnh (cờ recommended_at, gỡ được),
- số tài khoản đặt làm nghĩa chính (main_count): mạnh, tăng theo log,
- số người đã học nghĩa này (learner_count, tracking/stats.py): nhẹ, tăng theo log.
Điểm bằng nhau thì giữ thứ tự AI trả ra (id). Lượt xem, like không tính.

Cả công thức là một biểu thức SQL nên cập nhật bằng một lệnh UPDATE, không đọc từng dòng.
"""
from django.db.models import Case, F, FloatField, IntegerField, Value, When
from django.db.models.functions import Cast, Greatest, Ln

from ai.models import AISense

RECOMMEND_BONUS = 1000  # hơn 1000 tài khoản đặt chính (100 * ln 1001 = 691) mới ngang một đề xuất của admin
MAIN_WEIGHT = 100
LEARNER_WEIGHT = 20


def score_expression():
    recommended = Case(
        When(recommended_at__isnull=False, then=Value(float(RECOMMEND_BONUS))), default=Value(0.0), output_field=FloatField(),
    )
    main = Value(float(MAIN_WEIGHT)) * Ln(Greatest(F('main_count'), Value(0)) + Value(1.0))
    learners = Value(float(LEARNER_WEIGHT)) * Ln(Greatest(F('learner_count'), Value(0)) + Value(1.0))
    return Cast(Cast(recommended + main + learners, FloatField()), IntegerField())


def refresh_sense_scores(*conditions, **lookup):
    """Tính lại score cho các nghĩa khớp điều kiện (vd pk=..., word_id=..., Q(id=x) | Q(original_id=x)); không điều kiện = mọi nghĩa."""
    return AISense.objects.filter(*conditions, **lookup).update(score=score_expression())
