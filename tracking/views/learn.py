"""API chọn thẻ cho phiên học. Luật chọn ở tracking/learn.py."""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ai.models import AISense
from ai.validators import is_lang, is_uuid
from core.bundle import sense_tables
from core.models import UserCollection
from tracking.learn import original_key, pick_session

MAX_NEW = 50
DEFAULT_NEW = 6


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def learn_session(request):
    """
    Query: collection_id (bộ đã tải), user_lang, count (số nghĩa mới, mặc định 6, tối đa 50).
    Trả {due: [...], new: [...], due_count, new_available, day_ends_at, words, senses, sense_metadata, pending}:
    due = thẻ đến hạn ôn (học lại trước), new = nghĩa mới; words / senses / sense_metadata theo cột SQLite như bundle.
    """
    params = request.query_params
    collection_id, user_language_code = params.get('collection_id'), params.get('user_lang')
    if not is_uuid(collection_id) or not is_lang(user_language_code):
        return Response({'detail': 'collection_id, user_lang are required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        count = max(0, min(int(params.get('count', DEFAULT_NEW)), MAX_NEW))
    except ValueError:
        return Response({'detail': 'count must be a number'}, status=status.HTTP_400_BAD_REQUEST)

    user_collection = UserCollection.objects.filter(
        user=request.user, collection_id=collection_id, is_deleted=False, collection__isnull=False,
    ).first()
    if user_collection is None:
        return Response({'detail': 'Collection not downloaded'}, status=status.HTTP_404_NOT_FOUND)

    due, fresh, stats = pick_session(request.user, user_collection, count)

    def card(item, progress, chance):
        return {
            'item_id': item.id, 'sense_id': item.sense_id, 'original_sense_id': original_key(item),
            'level': progress.level if progress else 0,
            'next_review_at': progress.next_review_at if progress else None,
            'retrievability': round(chance, 4) if chance is not None else None,
        }

    sense_ids = [item.sense_id for item, _, _ in due + fresh]
    senses = list(AISense.objects.filter(id__in=sense_ids).select_related('word', 'metadata', 'original').order_by('id'))
    return Response({
        'collection_id': user_collection.collection_id,
        'due': [card(*entry) for entry in due],
        'new': [card(*entry) for entry in fresh],
        **stats,
        **sense_tables(senses, user_language_code),
    })
