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


def current_items(collection_id, version, user=None):
    """
    Các dòng đang hiệu lực của 1 bộ với 1 người xem: mỗi từ (original_id) lấy dòng nháp của chính user nếu có,
    không thì bản đã phát hành mới nhất tới `version`. Còn chứa dòng xoá (is_deleted), người gọi tự lọc.
    Nháp của người khác không bao giờ lọt ra.
    """
    from core.models import CollectionItem

    visible = Q(released=True, version__lte=version)
    if user is not None and user.is_authenticated:
        visible |= Q(released=False, created_by=user)
    return (
        CollectionItem.objects.filter(collection_id=collection_id).filter(visible)
        .order_by('original_id', 'released', '-version', '-created_at').distinct('original_id')
    )


def live_items(collection_id, version, user=None):
    """current_items bỏ dòng xoá, theo thứ tự hiển thị (display_order)."""
    rows = current_items(collection_id, version, user).select_related('sense')
    return sorted((row for row in rows if not row.is_deleted), key=lambda row: (row.display_order or 0, row.created_at))
