from django.db.models import Q
from django.db.models import Prefetch

from ai.models.AISense import AISense
from ai.models.AIWord import AIWord
from utils.utils.sense_handle import deep_merge


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


def published_sense_ids():
    """Nghĩa nằm trong 1 bộ từ đã phát hành (dòng đã phát hành): ai cũng xem / thêm vào bộ / tách bản sửa được."""
    from core.models import CollectionItem  # import muộn: core.models import ngược lại ai.models
    return (
        CollectionItem.objects.filter(released=True, collection__released=True)
        .exclude(collection__is_deleted=True).exclude(is_deleted=True).values('sense_id')
    )


def not_pending_proposal_q():
    """Không phải nghĩa đề xuất đang chờ duyệt. Khoá JSON vắng thì so sánh ra NULL nên phải xét isnull riêng (~Q một mình sẽ loại cả nghĩa thường)."""
    return Q(ai_review__proposal__status__isnull=True) | ~Q(ai_review__proposal__status='pending')


def visible_sense_q(user):
    """
    Điều kiện nghĩa user này được thấy: nghĩa chính thức, nghĩa của chính mình, và nghĩa đã đóng băng trong bộ đã phát hành
    (nghĩa người khác đã upload). Nghĩa tuỳ chỉnh chưa phát hành của người khác thì không.
    """
    # Nghĩa người dùng đề xuất đang chờ admin duyệt chỉ tác giả thấy, kể cả khi đã nằm trong bộ phát hành
    visible = Q(is_official=True) | (Q(is_frozen=True, id__in=published_sense_ids()) & not_pending_proposal_q())
    if user is not None and user.is_authenticated:
        visible |= Q(created_by=user)
    return visible


def visible_senses(user):
    queryset = AISense.objects.exclude(is_deleted=True).exclude(is_active=False)
    if user is not None and user.is_authenticated and user.is_staff:
        return queryset
    return queryset.filter(visible_sense_q(user))


def hidden_sense_ids(user, ids=None):
    """Nghĩa (thường là nghĩa chính thức) user đã ẩn với riêng mình."""
    from interact.models import UserInteraction
    rows = UserInteraction.objects.filter(user=user, interaction_type='hide', target_type='sense', status=True)
    if ids is not None:
        rows = rows.filter(target_id__in=list(ids))
    return {str(target) for target in rows.values_list('target_id', flat=True)}


def banned_sense_ids(ids):
    """
    Nghĩa không được đưa tới user nữa dù đã tải / đang học: bị ban (is_active=False), thuộc từ bị ban,
    hoặc nghĩa riêng dạng delta mà bản gốc bị ban.
    """
    ids = [i for i in ids if i]
    if not ids:
        return set()
    rows = AISense.objects.filter(id__in=ids).filter(
        Q(is_active=False) | Q(word__status='BANNED') | (Q(original__is_active=False) & (Q(contents={}) | Q(contents__isnull=True)))
    )
    return {str(i) for i in rows.values_list('id', flat=True)}


def user_senses(user, word_id):
    """Nghĩa tuỳ chỉnh của user cho 1 từ, mới nhất trước."""
    return (
        AISense.objects.filter(word_id=word_id, created_by=user, is_official=False)
        .exclude(is_deleted=True).exclude(is_active=False)
        .select_related('metadata', 'original').order_by('-id')
    )


def personalize_word(data, user):
    """
    Ghép nghĩa riêng của user vào dữ liệu từ dùng chung (cache / DB) mà không đụng vào cache:
    - nghĩa riêng tách từ 1 nghĩa (original) thì THAY nghĩa đó, nghĩa riêng tạo mới thì THÊM; cả hai đứng đầu;
    - nghĩa user đã ẩn bị bỏ.
    Nghĩa riêng dạng delta không có contents: lấy contents của nghĩa gốc, app áp delta lên như khi xem 1 nghĩa.
    data['senses'] là dict {id: sense}. Trả dict mới.
    """
    from ai.serializers.AISense import AISenseSerializer

    if not user or not user.is_authenticated or not data or not data.get('id'):
        return data
    senses = data.get('senses') or {}
    customs = list(user_senses(user, data['id']))
    hidden = hidden_sense_ids(user, senses.keys()) if senses else set()
    if not customs and not hidden:
        return data

    merged = {}
    for custom in customs:
        item = AISenseSerializer(custom).data
        if not item.get('contents'):
            base = senses.get(str(custom.original_id)) if custom.original_id else None
            item['contents'] = (base or {}).get('contents') or (custom.original.contents if custom.original else {})
            delta = custom.delta if isinstance(custom.delta, dict) else {}
            if delta:
                # Áp delta ngay để app nhận nghĩa đã sửa; definition / usage user sửa chỉ giữ ngôn ngữ có trong delta
                merged_contents = deep_merge(delta, item['contents'])
                for key in ('definition', 'usage'):
                    if isinstance(delta.get(key), dict):
                        merged_contents[key] = dict(delta[key])
                item['contents'] = merged_contents
        if custom.original:  # bản tách cũ có thể chưa kế thừa các trường này
            for field in ('pos', 'level', 'register', 'ipas'):
                if not item.get(field):
                    item[field] = getattr(custom.original, field)
        merged[str(custom.id)] = item
    replaced = {str(custom.original_id) for custom in customs if custom.original_id}
    for sense_id, sense in senses.items():
        if sense_id not in replaced and sense_id not in hidden:
            merged[sense_id] = sense
    return {**data, 'senses': merged}

