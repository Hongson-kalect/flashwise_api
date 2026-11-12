import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Exists, F, Max, OuterRef, Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ai.bulk import apply_sense_choices, best_official_senses, clean_words, fold_forms, sort_words, start_words
from ai.models import AISense, AIWord
from ai.queries import visible_senses
from ai.validators import is_lang, is_uuid
from core.models import Collection, CollectionItem, UserCollection
from core.queries import live_items, offensive_blocked, visible_collections
from interact.likes import liked_versions, set_like, version_like_counts
from core.serializers.Collection import (
    CollectionBasicSerializer, CollectionDetailSerializer, CollectionListSerializer, CollectionSerializer,
)
from utils.utils.normalize import lookup_entry_problem, normalize, normalize_lookup, is_lookup_word  # noqa: F401  (nhiều nơi vẫn import normalize từ module này)
from utils.utils.permissions import IsOwnerOrAdmin, NotBanned, consume_daily_quota
from core.scores import refresh_collection_scores
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from utils.utils.uuidv7 import generate_uuid7

logger = logging.getLogger(__name__)

MAX_COLLECTION_SENSES = 2000
COLLECTION_ORDERINGS = ('like_count', '-like_count', 'downloaded_count', '-downloaded_count', 'created_at', '-created_at', 'score', '-score',
                        'view_count', '-view_count')
# Tên dễ nhớ cho app: popular = điểm xếp hạng (core/scores.py), newest = mới nhất
ORDERING_ALIASES = {'popular': '-score', 'newest': '-created_at'}
VIEW_DEDUPE_SECONDS = 24 * 3600


def make_items(collection_id, senses, start=0, created_by=None):
    # original_id luôn trỏ về nghĩa gốc ổn định: tiến độ học của user gắn với id này nên nghĩa chính thức (không có original) chính là gốc của nó
    return [
        CollectionItem(
            collection_id=collection_id, sense_id=sense.id, original_id=sense.original_id or sense.id,
            value=sense.word_value, display_order=start + index, created_by=created_by,
        )
        for index, sense in enumerate(senses)
    ]


def parse_tag_ids(raw):
    """Query tag=id1,id2 -> danh sách UUID hợp lệ (bỏ giá trị sai)."""
    return [part for part in (raw or '').split(',') if is_uuid(part.strip())][:20]


def count_view(request, collection):
    """+1 lượt xem, mỗi người (hoặc IP nếu chưa đăng nhập) tính 1 lần / bộ / ngày; tác giả xem bộ mình không tính."""
    user = request.user
    if user.is_authenticated and collection.created_by_id == user.pk:
        return
    viewer = f'u{user.pk}' if user.is_authenticated else f"ip{request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')}"
    try:
        first = cache.add(f'collection_view:{collection.pk}:{viewer}', 1, VIEW_DEDUPE_SECONDS)
    except Exception:
        return  # cache lỗi: bỏ đếm, không làm hỏng việc xem bộ
    if first:
        Collection.objects.filter(pk=collection.pk).update(view_count=F('view_count') + 1)


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
            params = self.request.query_params
            if params.get('lang'):
                queryset = queryset.filter(language_code=params['lang'])
            if params.get('official') in ('true', 'false'):
                queryset = queryset.filter(is_official=params['official'] == 'true')
            if params.get('mine') == 'true' and self.request.user.is_authenticated:
                queryset = queryset.filter(created_by=self.request.user)
            else:
                # Danh sách công khai (Khám phá): chỉ bộ chính thức / đã phát hành, không lộ nháp hay bộ đã thu hồi của chính mình
                queryset = queryset.filter(Q(is_official=True) | Q(released=True))
            tag_ids = parse_tag_ids(params.get('tag'))
            if tag_ids:
                queryset = queryset.filter(tags__id__in=tag_ids).distinct()
            q = (params.get('q') or '').strip()[:100]
            if q:
                queryset = queryset.filter(name__icontains=q)  # có GinIndex trigram trên name nên icontains không quét cả bảng
            ordering = ORDERING_ALIASES.get(params.get('ordering'), params.get('ordering'))
            if ordering in COLLECTION_ORDERINGS:
                queryset = queryset.order_by(ordering, '-id')
            else:
                queryset = queryset.order_by('-score', '-created_at', '-id')  # core/scores.py
            # Bộ chứa nghĩa thô tục (nghĩa is_offensive): người chưa bật `show_offensive` không thấy / không tìm được; người đã bật thấy kèm cờ để app cảnh báo
            from user.prefs import allow_offensive

            queryset = queryset.annotate(has_offensive=Exists(
                CollectionItem.objects.filter(collection_id=OuterRef('pk'), released=True, sense__is_offensive=True).exclude(is_deleted=True)
            ))
            if not allow_offensive(self.request.user) and not params.get('mine') == 'true':
                queryset = queryset.filter(has_offensive=False)
            # Số từ của bản đang hiệu lực (item_count, tính lúc tạo / phát hành); đếm thẳng bảng trung gian sẽ cộng cả nháp và mọi version
            queryset = queryset.annotate(senses_count=F('item_count')).prefetch_related('tags')
        return queryset

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if offensive_blocked(request.user, instance):
            return Response({"message": "Collection contains offensive content", "errorCode": "offensive_collection"}, status=status.HTTP_403_FORBIDDEN)
        count_view(request, instance)
        if instance.pending_words:
            instance = self._resolve_pending(instance)
        # Bản phát hành mới nhất; người xem có nháp trong bộ (tác giả, người đã tải) thì nháp của họ thắng. Nháp người khác không lộ ra
        items = live_items(instance.id, instance.version, request.user)
        return Response(CollectionDetailSerializer(instance, context={'items': items, 'request': request}).data)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.released and not request.user.is_staff:
            # Người đã tải đang thấy tên / mô tả của bản phát hành: đổi qua upload (title, desc, image) để ghi vào lịch sử version
            return Response(
                {"message": "Released collection info changes via upload (title, desc, image)", "errorCode": "collection_released"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        Bộ chưa phát hành: xoá mềm. Bộ đã phát hành mà tác giả xoá: chỉ ẩn khỏi danh sách công khai / không tải mới được
        (is_active=False), người đã tải vẫn giữ và học tiếp. Admin xoá mềm bất kỳ bộ nào. Bộ cũng bị gỡ khỏi thư viện của người xoá.
        """
        instance = self.get_object()
        with transaction.atomic():
            if instance.released and not request.user.is_staff:
                instance.is_active = False
                instance.save(update_fields=['is_active', 'updated_at'])
            else:
                instance.is_deleted = True
                instance.save(update_fields=['is_deleted', 'updated_at'])
            UserCollection.objects.filter(collection=instance, user=request.user).exclude(is_deleted=True).update(is_deleted=True)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'])
    def versions(self, request, pk=None):
        """
        Lịch sử phát hành: mỗi version có title, desc, image, changelog {added, updated, deleted}, released_at và like_count
        của riêng version đó. Mới nhất trước. Kèm like_count tổng (số người) và liked_versions của người xem.
        """
        collection = self.get_object()
        likes = version_like_counts('collection', collection.pk)
        versions = [{**meta, 'like_count': likes.get(meta.get('version'), 0)} for meta in reversed(collection.metadatas or []) if isinstance(meta, dict)]
        return Response({
            "version": collection.version, "versions": versions, "like_count": collection.like_count,
            "liked_versions": liked_versions(request.user, 'collection', collection.pk),
        })

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
                elif word.status in ('REJECTED', 'INVALID', 'BANNED'):
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

        # Chỉ được đưa vào bộ nghĩa chính thức, nghĩa của mình hoặc nghĩa người khác đã upload
        senses = {
            str(sense.id): sense
            for sense in visible_senses(user).filter(id__in=sense_ids)
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

    @action(detail=True, methods=['post', 'delete'], permission_classes=[IsAuthenticated, NotBanned])
    def like(self, request, pk=None):
        """
        POST: thích 1 version của bộ (`version` trong body hoặc query, mặc định version hiện tại), DELETE: bỏ thích version đó
        (không gửi version thì bỏ mọi version). Mỗi version đếm riêng, 1 người thích được nhiều version;
        like_count của bộ = số người thích ít nhất 1 version. Gọi lại nhiều lần không đếm trùng.
        Trả {liked, liked_versions, like_count, version, version_like_count}.
        """
        collection = self.get_object()
        want = request.method == 'POST'
        raw = request.data.get('version') if hasattr(request.data, 'get') else None
        raw = raw if raw is not None else request.query_params.get('version')
        if raw is None:
            version = collection.version if want else None
        else:
            try:
                version = int(raw)
            except (TypeError, ValueError):
                return _bad_request("Invalid version")
            # Version đã phát hành (1..hiện tại), hoặc 0 khi bộ chưa từng phát hành
            if not (1 <= version <= collection.version or version == collection.version):
                return _bad_request("Unknown version")
        versions, like_count = set_like(request.user, Collection, 'collection', collection.pk, want, version)
        refresh_collection_scores(pk=collection.pk)
        return Response({
            'liked': bool(versions), 'liked_versions': versions, 'like_count': like_count, 'version': version,
            'version_like_count': None if version is None else version_like_counts('collection', collection.pk).get(version, 0),
        })

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
        cleaned = clean_words(words)
        if not cleaned:
            return _bad_request("No valid words")
        if len(cleaned) > settings.BULK_MAX_WORDS_PER_REQUEST:
            return _bad_request("At most %s words per request" % settings.BULK_MAX_WORDS_PER_REQUEST)
        cleaned, form_of = fold_forms(cleaned, language_code)  # dạng chia -> từ gốc (killed -> kill); bộ chứa nghĩa của từ gốc

        found, invalid, pending, to_start, existing = sort_words(cleaned, language_code)
        apply_sense_choices(found, data.get('senses'), language_code)  # nghĩa người dùng đã chọn khi gợi ý (từ đã có)
        # Mỗi từ mới tốn tiền nên tính vào hạn mức theo ngày
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
        start_words(user, to_start, existing, language_code, user_language_code, collection_id=collection.id)

        return Response(
            {"user_collection": user_collection.id, **CollectionBasicSerializer(collection).data, "pending_words": pending, "invalid_words": invalid, "form_of": form_of,
             "offensive_words": sorted(w for w, s in found.items() if s.is_offensive)},
            status=status.HTTP_201_CREATED,
        )
