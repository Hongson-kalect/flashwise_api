"""API chọn thẻ: học mới và ôn tập tách riêng. Luật chọn ở tracking/learn.py."""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ai.models import AISense
from ai.queries import banned_sense_ids
from ai.validators import is_lang, is_uuid
from core.bundle import sense_tables
from core.models import UserCollection
from core.views.UserCollection import collection_banned_response
from interact.moderation import collection_ban
from tracking.learn import NEW_MODES, REVIEW_MODES, original_key, pick_new, pick_review


def _bad(message):
    return Response({'detail': message}, status=status.HTTP_400_BAD_REQUEST)


def _params(request, modes):
    """Tham số chung: user_lang, mode, count. Trả (user_lang, mode, count) hoặc Response lỗi."""
    params = request.query_params
    user_language_code = params.get('user_lang')
    if not is_lang(user_language_code):
        return _bad('user_lang is required')
    mode = params.get('mode', 'normal')
    if mode not in modes:
        return _bad(f'mode must be one of {sorted(modes)}')
    try:
        count = max(0, min(int(params.get('count', modes[mode]['count'])), modes[mode]['max']))
    except ValueError:
        return _bad('count must be a number')
    return user_language_code, mode, count


def _exclude_words(request):
    """Query exclude_words: id từ (UUID, ngăn cách bằng dấu phẩy) đã có trong phiên. Trả set hoặc Response lỗi."""
    raw = [w.strip() for w in (request.query_params.get('exclude_words') or '').split(',') if w.strip()]
    if not all(is_uuid(w) for w in raw):
        return _bad('exclude_words must be comma separated UUIDs')
    return set(raw)


def _user_collection(request, required):
    """Bộ user đã tải theo collection_id (id bộ gốc). Trả (bộ hoặc None, Response lỗi hoặc None)."""
    collection_id = request.query_params.get('collection_id')
    if not collection_id:
        return None, (_bad('collection_id is required') if required else None)
    if not is_uuid(collection_id):
        return None, _bad('collection_id must be a UUID')
    user_collection = UserCollection.objects.filter(
        user=request.user, collection_id=collection_id, is_deleted=False, collection__isnull=False,
    ).first()
    if user_collection is None:
        return None, Response({'detail': 'Collection not downloaded'}, status=status.HTTP_404_NOT_FOUND)
    if collection_ban(user_collection.collection):
        return None, collection_banned_response(user_collection.collection)
    return user_collection, None


def _with_senses(cards, user_language_code):
    """Thêm bảng words / senses / sense_metadata (như bundle) cho các thẻ; bỏ thẻ mà nghĩa không còn hoặc bị ban."""
    banned = banned_sense_ids({c['sense_id'] for c in cards})
    senses = list(
        AISense.objects.filter(id__in=[c['sense_id'] for c in cards]).exclude(id__in=banned)
        .select_related('word', 'metadata', 'original').order_by('id')
    )
    existing = {str(s.id) for s in senses}
    return [c for c in cards if str(c['sense_id']) in existing], sense_tables(senses, user_language_code)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def learn_new(request):
    """
    Nghĩa mới của 1 bộ. Query: collection_id (bộ đã tải), user_lang, mode (normal), count (mặc định 6, tối đa 50),
    exclude_words (id từ đã có trong phiên, ngăn cách bằng dấu phẩy: nghĩa của các từ đó dời sang lần sau).
    Trả {mode, collection_id, new: [thẻ], new_available, words, senses, sense_metadata, pending}.
    """
    parsed = _params(request, NEW_MODES)
    if isinstance(parsed, Response):
        return parsed
    user_language_code, mode, count = parsed
    user_collection, error = _user_collection(request, required=True)
    if error:
        return error

    exclude = _exclude_words(request)
    if isinstance(exclude, Response):
        return exclude
    items, available = pick_new(request.user, user_collection, count, exclude)
    cards = [{'item_id': i.id, 'sense_id': i.sense_id, 'original_sense_id': original_key(i), 'level': 0} for i in items]
    cards, tables = _with_senses(cards, user_language_code)
    return Response({'mode': mode, 'collection_id': user_collection.collection_id, 'new': cards, 'new_available': available, **tables})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def learn_review(request):
    """
    Thẻ đến hạn ôn. Query: user_lang, mode (normal: như học mới, quick: vuốt / đọc, chỉ thẻ đủ cấp swipe_min_level),
    count (normal mặc định 6 tối đa 50, quick 30 / 200), collection_id (tuỳ chọn: chỉ nghĩa trong bộ đó, bỏ trống = mọi nghĩa đang học),
    exclude_words (như học mới). Không 2 nghĩa cùng 1 từ trong 1 lượt.
    Trả {mode, collection_id, due: [thẻ], due_count (hợp kiểu ôn), due_total (mọi thẻ đến hạn), day_ends_at, words, senses, sense_metadata, pending}.
    """
    parsed = _params(request, REVIEW_MODES)
    if isinstance(parsed, Response):
        return parsed
    user_language_code, mode, count = parsed
    user_collection, error = _user_collection(request, required=False)
    if error:
        return error

    exclude = _exclude_words(request)
    if isinstance(exclude, Response):
        return exclude
    picked, due_total, due_count, cutoff = pick_review(request.user, mode, count, user_collection, exclude_words=exclude)
    cards = [{
        'item_id': item.id if item else None, 'sense_id': sense_id, 'original_sense_id': str(progress.original_sense_id),
        'level': progress.level, 'next_review_at': progress.next_review_at, 'retrievability': round(chance, 4),
    } for progress, sense_id, item, chance in picked]
    cards, tables = _with_senses(cards, user_language_code)
    return Response({
        'mode': mode, 'collection_id': user_collection.collection_id if user_collection else None,
        'due': cards, 'due_count': due_count, 'due_total': due_total, 'day_ends_at': cutoff, **tables,
    })


def _complete(request, kind):
    from tracking.session import SessionError, complete_session
    try:
        result = complete_session(request.user, kind, request.data.get('mode', 'normal'), request.data)
    except SessionError as e:
        return _bad(f'invalid:{e}')
    return Response(result, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def learn_new_complete(request):
    """
    Xác nhận phiên học mới (tracking/session.py). Body: {mode: normal, session: {id, start_at, end_at, time, collection_id, methods},
    cards: [{sense_id, original_sense_id, log_id, rating (1-4) | known: true, mode, hint_count, attempts, duration_ms, reviewed_at,
    steps: [{id, rating, mode, hint_count, attempts, duration_ms, reviewed_at}]}]}.
    Trả {duplicate, session, progress: [dòng user_sense_progress], rejected: [{sense_id, reason}]}.
    """
    return _complete(request, 'learn')


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def learn_review_complete(request):
    """Xác nhận phiên ôn tập, body như học mới (không có known), mode normal | quick."""
    return _complete(request, 'review')
