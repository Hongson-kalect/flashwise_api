import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Max, Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ai.generation import GenerationError, start_word_generation
from ai.models import AISense, AIWord
from ai.validators import is_lang, is_uuid
from core.models import Collection, CollectionItem, UserCollection
from core.queries import visible_collections
from interact.models import UserInteraction
from core.serializers.Collection import (
    CollectionBasicSerializer, CollectionDetailSerializer, CollectionListSerializer, CollectionSerializer,
)
from utils.redis.client import get_queue_client
from utils.redis.word_init import WordCacheManager
from utils.utils.normalize import normalize, normalize_lookup, is_lookup_word  # noqa: F401  (nhiều nơi vẫn import normalize từ module này)
from utils.utils.permissions import IsOwnerOrAdmin, consume_daily_quota
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from utils.utils.uuidv7 import generate_uuid7

logger = logging.getLogger(__name__)

MAX_COLLECTION_SENSES = 2000


def best_official_senses(words, language_code):
    """Nghĩa chính thức có điểm cao nhất của mỗi từ trong ngôn ngữ đó (không lấy nghĩa tuỳ chỉnh của ai)."""
    return (
        AISense.objects.filter(is_official=True, language_code=language_code, word_value__in=list(words))
        .exclude(is_deleted=True).exclude(is_active=False)
        .order_by('word_value', '-score', 'id').distinct('word_value')
    )


def make_items(collection_id, senses, start=0, created_by=None):
    # original_id luôn trỏ về nghĩa gốc ổn định: tiến độ học của user gắn với id này nên nghĩa chính thức (không có original) chính là gốc của nó
    return [
        CollectionItem(
            collection_id=collection_id, sense_id=sense.id, original_id=sense.original_id or sense.id,
            value=sense.word_value, display_order=start + index, created_by=created_by,
        )
        for index, sense in enumerate(senses)
    ]


def _bad_request(message, **extra):
    return Response({"message": message, **extra}, status=status.HTTP_400_BAD_REQUEST)


class CollectionViewSet(SoftDeleteViewSet):
    queryset = Collection.objects.all()
    serializer_class = CollectionSerializer
    # Xem: ai cũng được (bộ chính thức / đã phát hành / của chính mình). Tạo: phải đăng nhập. Sửa / xoá: người tạo hoặc admin
    permission_classes = [IsOwnerOrAdmin]

    def get_serializer_class(self):
        if self.action == 'list':
            return CollectionListSerializer
        if self.action == 'retrieve':
            return CollectionDetailSerializer
        return CollectionSerializer

    def get_queryset(self):
        queryset = visible_collections(self.request.user)
        if self.action == 'list':
            # Chỉ SELECT bảng Collection + COUNT bảng trung gian bằng SQL, không kéo chi tiết nghĩa lên RAM
            queryset = queryset.annotate(senses_count=Count('collectionitem', filter=~Q(collectionitem__is_deleted=True)))
        return queryset

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.pending_words:
            instance = self._resolve_pending(instance)
        items = list(instance.collectionitem_set.exclude(is_deleted=True).select_related('sense').order_by('display_order', 'created_at'))
        return Response(CollectionDetailSerializer(instance, context={'items': items, 'request': request}).data)

    def _resolve_pending(self, instance):
        """
        Từ đang chờ AI: tạo xong thì thêm vào bộ, bị từ chối thì chuyển sang invalid_words, còn lại vẫn chờ.
        Ai xem bộ cũng kích hoạt được bước này nên chạy trong transaction có khoá dòng: 2 request đồng thời không thêm trùng.
        """
        with transaction.atomic():
            collection = Collection.objects.select_for_update().get(pk=instance.pk)
            pending = list(collection.pending_words or [])
            if not pending:
                return collection

            lookup = {'value__in': pending}
            if collection.language_code:
                lookup['language_code'] = collection.language_code
            done, invalid = set(), []
            for word in AIWord.objects.filter(**lookup).exclude(is_deleted=True):
                if word.status == 'COMPLETED':
                    done.add(word.value)
                elif word.status in ('REJECTED', 'INVALID'):
                    invalid.append(word.value)

            senses = list(best_official_senses(done, collection.language_code)) if done else []
            # Từ đã nằm trong bộ (bản nháp hay bản phát hành) thì không thêm lại
            present = set(CollectionItem.objects.filter(collection=collection).values_list('original_id', flat=True))
            to_add = [sense for sense in senses if (sense.original_id or sense.id) not in present]
            resolved = {sense.word_value for sense in senses} | set(invalid)
            if not resolved:
                return collection

            last = CollectionItem.objects.filter(collection=collection).aggregate(last=Max('display_order'))['last']
            # Giao cho người tạo bộ như bản nháp của họ (dòng không có created_by thì không ai thấy)
            CollectionItem.objects.bulk_create(make_items(collection.id, to_add, 0 if last is None else last + 1, collection.created_by))
            collection.pending_words = [w for w in pending if w not in resolved]
            collection.invalid_words = list(dict.fromkeys([*(collection.invalid_words or []), *invalid]))
            collection.item_count = CollectionItem.objects.filter(collection=collection).exclude(is_deleted=True).values('original_id').distinct().count()
            collection.save(update_fields=['pending_words', 'invalid_words', 'item_count', 'updated_at'])
            return collection

    def create(self, request, *args, **kwargs):
        """Tạo bộ từ từ danh sách nghĩa có sẵn. params: name, description, tags, language_code, senses=[{id}]."""
        user = request.user
        senses_data = request.data.get('senses', [])
        if not isinstance(senses_data, list) or len(senses_data) > MAX_COLLECTION_SENSES:
            return _bad_request("senses must be a list of at most %s items" % MAX_COLLECTION_SENSES)

        sense_ids = []
        for entry in senses_data:
            sense_id = entry.get('id') if isinstance(entry, dict) else None
            if not is_uuid(sense_id):
                return _bad_request("Invalid sense id")
            sense_ids.append(str(uuid.UUID(str(sense_id))))
        sense_ids = list(dict.fromkeys(sense_ids))

        # Chỉ được đưa vào bộ nghĩa chính thức hoặc nghĩa của chính mình
        senses = {
            str(sense.id): sense
            for sense in AISense.objects.filter(id__in=sense_ids).filter(Q(is_official=True) | Q(created_by=user)).exclude(is_deleted=True)
        }
        unknown = [sense_id for sense_id in sense_ids if sense_id not in senses]
        if unknown:
            return _bad_request("Unknown sense", ids=unknown[:20])

        payload = request.data.dict() if hasattr(request.data, 'dict') else dict(request.data)
        payload.pop('senses', None)
        if hasattr(request.data, 'getlist') and 'tags' in request.data:
            payload['tags'] = request.data.getlist('tags')
        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            collection = serializer.save(id=generate_uuid7(), created_by=user, updated_by=user, item_count=len(sense_ids))
            user_collection = UserCollection.objects.create(user=user, collection=collection, name=collection.name, created_by=user)
            CollectionItem.objects.bulk_create(make_items(collection.id, [senses[sense_id] for sense_id in sense_ids], 0, user))

        return Response({"user_collection": user_collection.id, **serializer.data}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post', 'delete'], permission_classes=[IsAuthenticated])
    def like(self, request, pk=None):
        """POST: thích bộ từ, DELETE: bỏ thích. Gọi lại nhiều lần không đếm trùng. Trả {liked, like_count}."""
        collection = self.get_object()
        want = request.method == 'POST'
        with transaction.atomic():
            interaction, _ = UserInteraction.objects.select_for_update().get_or_create(
                user=request.user, interaction_type='like', target_type='collection', target_id=collection.pk,
                defaults={'status': False},
            )
            if bool(interaction.status) != want:
                interaction.status = want
                interaction.save(update_fields=['status', 'updated_at'])
                Collection.objects.filter(pk=collection.pk).update(like_count=F('like_count') + (1 if want else -1))
        collection.refresh_from_db(fields=['like_count'])
        return Response({'liked': want, 'like_count': max(collection.like_count, 0)})

    @action(detail=False, methods=['post'], url_path='bulk-create', permission_classes=[IsAuthenticated])
    def create_bulk(self, request, *args, **kwargs):
        """
        Tạo bộ từ từ danh sách từ (chưa cần có sẵn trong hệ thống). params: name, description, lang, user_lang, words.
        Từ đã có nghĩa chính thức vào bộ ngay; từ bị từ chối vào invalid_words; từ còn lại vào pending_words
        (từ chưa từng có thì đẩy việc tạo bằng AI), tạo xong sẽ được thêm vào bộ khi xem bộ (retrieve).
        """
        user = request.user
        data = request.data
        name, description = data.get('name'), data.get('description') or ''
        language_code, user_language_code, words = data.get('lang'), data.get('user_lang'), data.get('words')

        if not isinstance(name, str) or not name.strip() or len(name) > 255 or not isinstance(description, str) or len(description) > 2000:
            return _bad_request("Invalid name or description")
        if not is_lang(language_code) or not is_lang(user_language_code) or not isinstance(words, list):
            return _bad_request("lang, user_lang and words are required")

        # Chuẩn hoá giống get-word / worker (không bỏ ký tự): 'c++' vẫn là 'c++', trùng đúng từ user tra lẻ
        cleaned = list(dict.fromkeys(w for w in (normalize_lookup(w) for w in words if isinstance(w, str)) if is_lookup_word(w)))
        if not cleaned:
            return _bad_request("No valid words")
        if len(cleaned) > settings.BULK_MAX_WORDS_PER_REQUEST:
            return _bad_request("At most %s words per request" % settings.BULK_MAX_WORDS_PER_REQUEST)

        found = {sense.word_value: sense for sense in best_official_senses(cleaned, language_code)}
        missing = [w for w in cleaned if w not in found]
        existing = {w.value: w for w in AIWord.objects.filter(value__in=missing, language_code=language_code).exclude(is_deleted=True)} if missing else {}

        invalid = [w for w in missing if w in existing and existing[w].status in ('REJECTED', 'INVALID')]
        pending = [w for w in missing if w not in invalid]
        # Cần AI tạo: chưa có bản ghi nào, hoặc lần tạo trước lỗi. Mỗi từ mới tốn tiền nên tính vào hạn mức theo ngày
        to_start = [w for w in pending if w not in existing or existing[w].status == 'FAILED']
        if to_start and not consume_daily_quota(user, 'bulk_words', len(to_start), settings.BULK_WORDS_PER_DAY):
            return Response({"detail": "Daily new-word limit reached", "errorCode": "bulk_quota_exceeded"}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        with transaction.atomic():
            collection = Collection.objects.create(
                id=generate_uuid7(), name=name.strip(), description=description, language_code=language_code,
                is_official=False, created_by=user, updated_by=user,
                invalid_words=invalid, pending_words=pending, item_count=len(found),
            )
            user_collection = UserCollection.objects.create(user=user, collection=collection, name=collection.name, created_by=user)
            CollectionItem.objects.bulk_create(make_items(collection.id, [found[w] for w in cleaned if w in found], 0, user))

        # Gọi AI sau khi DB đã commit
        cache_manager = WordCacheManager()
        queue = get_queue_client()
        for word in to_start:
            try:
                state, _ = start_word_generation(
                    cache_manager, word, language_code, user_language_code, lambda: user,
                    existing=existing.get(word), queue='redis_collection_word',
                )
            except GenerationError:
                continue  # từ này ở lại pending, lần sau tạo lại
            if state == 'started':
                queue.sadd(f'collection_id:{word}', str(collection.id))

        return Response(
            {"user_collection": user_collection.id, **CollectionBasicSerializer(collection).data, "pending_words": pending, "invalid_words": invalid},
            status=status.HTTP_201_CREATED,
        )
