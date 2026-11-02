"""
Khám phá bộ từ: chủ đề (tag), gợi ý bộ theo chủ đề quan tâm, thống kê bộ mình đã phát hành.
Chủ đề quan tâm lưu ở user_settings key `interest_tags` (danh sách id tag, app sync như các setting khác);
chưa đăng nhập / chưa sync thì app truyền thẳng qua query `tags`.
"""
from django.db.models import Count, F, IntegerField, Q, Sum, Value
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from ai.validators import is_lang
from core.models import Collection, UserCollection
from core.queries import visible_collections
from core.serializers.Collection import CollectionListSerializer
from core.views.Collection import parse_tag_ids
from user.models import UserSetting
from utils.models.Tag import Tag

INTEREST_KEY = 'interest_tags'
SUGGEST_DEFAULT, SUGGEST_MAX = 10, 30


def _public(queryset):
    """Bộ ai cũng thấy: chính thức hoặc đã phát hành (không tính bộ nháp của chính người xem)."""
    return queryset.filter(Q(is_official=True) | Q(released=True))


@api_view(['GET'])
@permission_classes([AllowAny])
def tags(request):
    """
    Chủ đề / tag. Query: lang (ngôn ngữ đang học: tag chung + tag riêng của ngôn ngữ đó), topics=true (chỉ chủ đề hệ thống,
    dùng ở màn khởi động), user_lang (tên hiển thị theo ngôn ngữ giao diện). Mỗi tag kèm collection_count (số bộ công khai).
    """
    params = request.query_params
    lang, user_lang = params.get('lang'), params.get('user_lang')
    rows = Tag.objects.exclude(is_deleted=True).exclude(is_active=False)
    count_filter = Q(collection__is_active=True) & ~Q(collection__is_deleted=True) & (Q(collection__is_official=True) | Q(collection__released=True))
    if is_lang(lang):
        rows = rows.filter(Q(languageCode=lang) | Q(languageCode__isnull=True) | Q(languageCode=''))
        count_filter &= Q(collection__language_code=lang)
    if params.get('topics') == 'true':
        rows = rows.filter(isSystemTag=True)
    rows = rows.annotate(collection_count=Count('collection', filter=count_filter, distinct=True)).order_by('display_order', 'name')
    return Response([{
        'id': str(tag.id), 'name': (tag.names or {}).get(user_lang) or tag.name, 'language_code': tag.languageCode or None,
        'is_topic': tag.isSystemTag, 'collection_count': tag.collection_count,
    } for tag in rows])


def interest_tags(request):
    """Chủ đề quan tâm: query tags nếu có, không thì setting interest_tags của user."""
    from_query = parse_tag_ids(request.query_params.get('tags'))
    if from_query or not request.user.is_authenticated:
        return from_query
    row = UserSetting.objects.filter(user=request.user, key=INTEREST_KEY).exclude(is_deleted=True).first()
    value = row.value if row else None
    return parse_tag_ids(','.join(str(v) for v in value)) if isinstance(value, list) else []


@api_view(['GET'])
@permission_classes([AllowAny])
def suggested_collections(request):
    """
    Gợi ý bộ cho Discover / Home. Query: lang (bắt buộc), limit (mặc định 10, tối đa 30), tags (ghi đè chủ đề quan tâm).
    Bộ trùng nhiều chủ đề quan tâm lên trước, rồi theo điểm xếp hạng (chất lượng là chính); bỏ bộ đã tải và bộ của mình.
    """
    lang = request.query_params.get('lang')
    if not is_lang(lang):
        return Response({'detail': 'lang is required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        limit = max(1, min(int(request.query_params.get('limit', SUGGEST_DEFAULT)), SUGGEST_MAX))
    except ValueError:
        return Response({'detail': 'limit must be a number'}, status=status.HTTP_400_BAD_REQUEST)

    interests = interest_tags(request)
    queryset = _public(visible_collections(request.user)).filter(language_code=lang)
    if request.user.is_authenticated:
        downloaded = UserCollection.objects.filter(user=request.user, collection__isnull=False).exclude(is_deleted=True).values('collection_id')
        queryset = queryset.exclude(id__in=downloaded).exclude(created_by=request.user)
    queryset = queryset.annotate(
        matched_tags=Count('tags', filter=Q(tags__id__in=interests), distinct=True) if interests else Value(0, output_field=IntegerField()),
        senses_count=F('item_count'),
    ).order_by('-matched_tags', '-score', '-created_at', '-id')[:limit]
    results = []
    for collection in queryset.prefetch_related('tags'):
        row = CollectionListSerializer(collection).data
        row['matched_tags'] = collection.matched_tags
        results.append(row)
    return Response({'interest_tags': interests, 'results': results})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_uploads(request):
    """
    Thống kê bộ mình đã phát hành (màn Upload): tổng số bộ, lượt xem / thích / tải, và từng bộ (mới nhất trước).
    is_active=false: bộ đã ẩn (tác giả xoá hoặc bị ban) nhưng vẫn tính vào thống kê.
    """
    rows = Collection.objects.filter(created_by=request.user, released=True).exclude(is_deleted=True).order_by('-created_at')
    totals = rows.aggregate(views=Sum('view_count'), likes=Sum('like_count'), downloads=Sum('downloaded_count'))
    return Response({
        'collection_count': rows.count(),
        'view_count': totals['views'] or 0, 'like_count': totals['likes'] or 0, 'downloaded_count': totals['downloads'] or 0,
        'collections': [{
            'id': str(c.id), 'name': c.name, 'image_url': c.image_url, 'version': c.version, 'is_active': c.is_active is not False,
            'view_count': c.view_count, 'like_count': c.like_count, 'downloaded_count': c.downloaded_count,
            'item_count': c.item_count, 'created_at': c.created_at,
        } for c in rows],
    })
