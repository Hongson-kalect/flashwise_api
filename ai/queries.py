from django.db.models import Prefetch

from ai.models.AISense import AISense
from ai.models.AIWord import AIWord


def official_senses():
    """
    Các nghĩa chính thức của 1 từ, kèm metadata trong cùng 1 query.
    Sắp theo id: UUIDv7 tăng dần theo thời điểm sinh nên giữ đúng thứ tự AI trả ra (nghĩa phổ biến nhất trước).
    is_active / is_deleted cho phép NULL nên loại trừ thay vì lọc bằng bằng.
    """
    return (
        AISense.objects.filter(is_official=True)
        .exclude(is_deleted=True)
        .exclude(is_active=False)
        .select_related('metadata')
        .order_by('id')
    )


def load_word(**lookup):
    """1 từ cùng các nghĩa chính thức (đã prefetch vào word.senses, serializer không phát sinh thêm query)."""
    return (
        AIWord.objects.filter(**lookup)
        .exclude(is_deleted=True)
        .exclude(is_active=False)
        .prefetch_related(Prefetch('senses', queryset=official_senses()))
        .first()
    )
