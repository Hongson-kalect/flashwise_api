from django.db.models import Q

from core.models import Collection


def visible_collections(user):
    """
    Bộ từ người dùng được xem / tải: bộ chính thức, bộ đã phát hành, và bộ do chính mình tạo (admin thấy hết).
    Bộ cá nhân của người khác không được lộ ra.
    """
    queryset = Collection.objects.filter(is_active=True).exclude(is_deleted=True)
    if user.is_authenticated and user.is_staff:
        return queryset
    visible = Q(is_official=True) | Q(released=True)
    if user.is_authenticated:
        visible |= Q(created_by=user)
    return queryset.filter(visible)
