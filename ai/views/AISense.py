import json
import logging
import re

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Case, Count, ExpressionWrapper, F, FloatField, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce, Greatest, Least, Ln
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from ai.jobs import queue_translation
from ai.sense_edit import apply_sense_edit
from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AIWord import AIWord
from ai.models import SenseFormExample
from ai import form_examples
from ai.forms import refresh_forms
from ai.proposals import create_proposed_sense, duplicate_sense, existing_senses_of
from ai.queries import not_pending_proposal_q, published_sense_ids, visible_sense_q
from core.bundle import pick_languages
from core.models import CollectionItem
from django.utils import timezone
from interact.likes import set_like
from interact.models import UserInteraction
from ai.serializers.AISense import AISenseSerializer
from ai.validators import MAX_DELTA_CHARS, METADATA_FIELDS, clean_contents, clean_metadata, is_lang, is_uuid
from asset.media import clean_media_param
from utils.ai import items_gate, sense_gate
from utils.utils.normalize import is_lookup_word, lookup_entry_problem, normalize_lookup
from utils.utils.permissions import IsAdminOrReadOnly, NotBanned, SenseItemsThrottle, SenseProposeThrottle
from user.reputation import record
from ai.scores import refresh_sense_scores
from interact.mains import set_main
from utils.redis.word_init import WordCacheManager
from utils.utils.best_effort import best_effort
from utils.utils.sense_handle import deep_merge, get_user_lang_sense
from utils.utils.soft_delete_viewset import SoftDeleteViewSet

logger = logging.getLogger(__name__)


def _bad_request(message):
    return Response({"message": message}, status=status.HTTP_400_BAD_REQUEST)


TOP_MAX = 100
TOP_CACHE_SECONDS = 600
TOP_MIN_REVIEWS = 20  # forget_rate: nghĩa ít lần chấm thì tỉ lệ dao động mạnh nên bỏ
TOP_SENSE_ORDERS = {
    'forgot': 'global_forget_count',
    'forget_rate': 'forget_rate',
    'learned': 'learner_count',
    'liked': 'like_count',
}


def promoted_contents(sense):
    """contents đầy đủ của nghĩa tuỳ chỉnh: delta áp lên nghĩa gốc; definition / usage user sửa chỉ giữ ngôn ngữ trong delta."""
    base = sense.contents or (sense.original.contents if sense.original else {}) or {}
    delta = sense.delta if isinstance(sense.delta, dict) else {}
    merged = deep_merge(delta, base) if delta else dict(base)
    for key in ('definition', 'usage'):
        if isinstance(delta.get(key), dict):
            merged[key] = dict(delta[key])
    return merged


def top_senses(language_code, by):
    """Nghĩa chính thức (thống kê học ghi trên mọi bản cùng gốc nên chỉ lấy bản chính thức để không trùng), giá trị > 0."""
    queryset = (
        AISense.objects.filter(is_official=True, language_code=language_code)
        .exclude(is_deleted=True).exclude(is_active=False).select_related('metadata')
    )
    if by == 'forget_rate':
        total = F('global_forget_count') + F('global_remember_count')
        queryset = queryset.annotate(reviews=total).filter(reviews__gte=TOP_MIN_REVIEWS, global_forget_count__gt=0).annotate(
            forget_rate=ExpressionWrapper(Cast('global_forget_count', FloatField()) / total, output_field=FloatField()),
        )
    else:
        queryset = queryset.filter(**{TOP_SENSE_ORDERS[by] + '__gt': 0})
    return queryset.order_by('-' + TOP_SENSE_ORDERS[by], '-global_forget_count', 'id')


def top_sense_row(sense, language_code, user_language_code):
    forgot, remembered = sense.global_forget_count, sense.global_remember_count
    langs = {language_code, user_language_code} - {None}
    return {
        'id': str(sense.id), 'word_id': str(sense.word_id), 'word_value': sense.word_value,
        'pos': sense.pos, 'level': sense.level, 'ipas': sense.ipas, 'image_preview': sense.image_preview,
        'contents': pick_languages(sense.contents or {}, langs),
        'global_forget_count': forgot, 'global_remember_count': remembered,
        'forget_rate': round(forgot / (forgot + remembered), 4) if forgot + remembered else 0.0,
        'learner_count': sense.learner_count, 'like_count': sense.like_count, 'global_difficulty': sense.global_difficulty,
    }


COMMUNITY_MAX = 50
COMMUNITY_ORDERS = ('popular', 'newest')
AUTHOR_MIN, AUTHOR_MAX = -50, 100  # như core/scores.py: kẹp uy tín tác giả rồi chia 10 (cộng / trừ rất nhẹ)


def community_senses(word_id, user):
    """
    Nghĩa tuỳ chỉnh người khác đã phát hành cho 1 từ (đóng băng và nằm trong bộ đã phát hành, như visible_sense_q),
    bỏ nghĩa của chính mình, nghĩa mình đã ẩn, nghĩa bị đánh dấu xấu. Kèm `rank`:
      3·ln(1+like) + 2·ln(1+số bản được tách từ nghĩa này) + uy tín tác giả/10 (kẹp -5..10) + 3 nếu đã verified.
    """
    from user.models import UserProfile

    reputation = Coalesce(
        Subquery(UserProfile.objects.filter(user_id=OuterRef('created_by_id')).values('reputation')[:1]), Value(0),
    )
    adopted = Coalesce(
        Subquery(
            AISense.objects.filter(previous_id=OuterRef('pk')).exclude(is_deleted=True)
            .values('previous_id').annotate(n=Count('pk')).values('n')[:1]
        ),
        Value(0),
    )
    queryset = (
        AISense.objects.filter(word_id=word_id, is_official=False, is_frozen=True, id__in=published_sense_ids())
        .exclude(is_deleted=True).exclude(is_active=False).exclude(is_offensive=True).exclude(quality_status='flagged')
        .filter(not_pending_proposal_q())
        .select_related('metadata')
        .annotate(
            adopted_count=Cast(adopted, IntegerField()),
            author_rep=reputation,
        )
        .annotate(
            rank=Value(3.0) * Ln(Greatest(F('like_count'), Value(0)) + Value(1.0))
            + Value(2.0) * Ln(F('adopted_count') + Value(1.0))
            + Least(Greatest(F('author_rep'), Value(AUTHOR_MIN)), Value(AUTHOR_MAX)) / Value(10.0)
            + Case(When(quality_status='verified', then=Value(3.0)), default=Value(0.0), output_field=FloatField()),
        )
    )
    if user is not None and user.is_authenticated:
        hidden = UserInteraction.objects.filter(user=user, target_type='sense', interaction_type='hide', status=True).values('target_id')
        queryset = queryset.exclude(created_by=user).exclude(id__in=hidden)
    return queryset


def community_row(sense, langs, liked_ids):
    """Một dòng gợi ý: nội dung đã gộp delta + chỉ các ngôn ngữ cần, kèm tín hiệu xếp hạng và tác giả."""
    from user.models import UserProfile

    profile = UserProfile.objects.filter(user_id=sense.created_by_id).only('id', 'full_name').first() if sense.created_by_id else None
    metadata = sense.metadata
    return {
        'id': str(sense.id), 'word_id': str(sense.word_id), 'word_value': sense.word_value,
        'pos': sense.pos, 'level': sense.level, 'ipas': sense.ipas, 'image_preview': sense.image_preview,
        'contents': pick_languages(promoted_contents(sense), langs),
        'metadata': {'advanced': metadata.advanced, 'tags': metadata.tags} if metadata else None,
        'like_count': sense.like_count, 'adopted_count': sense.adopted_count, 'quality_status': sense.quality_status,
        'liked': str(sense.id) in liked_ids,
        'author': {'id': str(profile.id), 'name': profile.full_name or None, 'reputation': sense.author_rep} if profile else None,
        'rank': round(sense.rank, 3), 'created_at': sense.created_at,
    }


def clear_word_cache(sense):
    """Nghĩa đổi thì bỏ cache Redis của từ để get-word đọc lại từ DB (cache lỗi không làm hỏng request)."""
    best_effort(WordCacheManager().cache_word_clear_specific, sense.language_code, sense.word_value)


class AISenseViewSet(SoftDeleteViewSet):
    queryset = AISense.objects.select_related('metadata', 'original')
    serializer_class = AISenseSerializer
    # CRUD trực tiếp lên sense hệ thống chỉ admin; user đề xuất nghĩa mới qua propose (cổng AI), sửa nghĩa của mình qua update-sense; create-sense chỉ admin
    permission_classes = [IsAdminOrReadOnly]

    def get_permissions(self):
        if self.action == 'destroy':
            return [IsAuthenticated()]  # user xoá nghĩa của chính mình, kiểm tra chủ trong destroy
        return super().get_permissions()

    def get_queryset(self):
        """Nghĩa chính thức, nghĩa của chính mình và nghĩa người khác đã upload trong bộ đã phát hành (admin thấy hết)."""
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_authenticated and user.is_staff:
            return queryset
        return queryset.filter(visible_sense_q(user))

    def destroy(self, request, *args, **kwargs):
        """
        Admin: xoá mềm bất kỳ nghĩa nào. User: chỉ nghĩa tuỳ chỉnh của mình chưa đóng băng (đã upload thì bộ của người khác
        đang trỏ tới nên không xoá được, dùng hide). Các dòng nháp của user trong bộ từ trỏ tới nghĩa này bị bỏ theo.
        """
        sense = self.get_object()
        user = request.user
        if not user.is_staff:
            if sense.is_official or sense.created_by_id != user.id:
                return Response({"message": "Only your own custom senses can be deleted, hide it instead"}, status=status.HTTP_403_FORBIDDEN)
            if sense.is_frozen:
                return Response({"message": "Published sense cannot be deleted, hide it instead", "errorCode": "sense_frozen"}, status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            sense.is_deleted = True
            sense.deleted_at = timezone.now()
            sense.updated_by = user
            sense.save(update_fields=['is_deleted', 'deleted_at', 'updated_by', 'updated_at'])
            CollectionItem.objects.filter(sense=sense, released=False, created_by=sense.created_by).delete()
        clear_word_cache(sense)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post', 'delete'], url_path='hide', permission_classes=[IsAuthenticated])
    def hide(self, request, pk=None):
        """
        POST: ẩn nghĩa khỏi get-word của riêng mình, DELETE: bỏ ẩn. Nghĩa tuỳ chỉnh của mình chưa đóng băng thì xoá chứ không ẩn;
        đã đóng băng (đã phát hành, người khác còn gọi theo id) thì chỉ ẩn được.
        """
        sense = self.get_object()
        if sense.created_by_id == request.user.id and not sense.is_official and not sense.is_frozen and request.method == 'POST':
            return _bad_request("Delete your own sense instead of hiding it")
        want = request.method == 'POST'
        UserInteraction.objects.update_or_create(
            user=request.user, interaction_type='hide', target_type='sense', target_id=sense.id,
            defaults={'status': want, 'created_by': request.user},
        )
        return Response({'hidden': want, 'sense_id': str(sense.id)})

    @action(detail=True, methods=['post', 'delete'], url_path='like', permission_classes=[IsAuthenticated, NotBanned])
    def like(self, request, pk=None):
        """POST: thích (tim) nghĩa, DELETE: bỏ thích. Mỗi bản nghĩa (chính thức / tuỳ chỉnh) đếm riêng, gọi lại không đếm trùng."""
        sense = self.get_object()
        versions, like_count = set_like(request.user, AISense, 'sense', sense.id, request.method == 'POST')
        clear_word_cache(sense)
        return Response({'liked': bool(versions), 'sense_id': str(sense.id), 'like_count': like_count})

    @action(detail=True, methods=['post', 'delete'], url_path='main', permission_classes=[IsAuthenticated, NotBanned])
    def main(self, request, pk=None):
        """
        POST: đặt nghĩa này làm nghĩa chính của từ cho mình (đứng đầu trang từ), DELETE: bỏ. Mỗi từ 1 nghĩa chính, đặt nghĩa khác thì
        nghĩa cũ bị thu hồi. Tài khoản thật (không phải khách) còn cộng vào điểm chung của nghĩa chính thức (main_count, score).
        Trả {main, sense_id, word_id, main_count}.
        """
        sense = self.get_object()
        main, main_count, touched = set_main(request.user, sense, request.method == 'POST')
        if touched:
            clear_word_cache(sense)
        return Response({'main': main, 'sense_id': str(sense.id), 'word_id': str(sense.word_id), 'main_count': main_count})

    @action(detail=True, methods=['post', 'delete'], url_path='recommend', permission_classes=[IsAdminUser])
    def recommend(self, request, pk=None):
        """Admin: POST đề xuất nghĩa này (score tăng rất mạnh), DELETE gỡ đề xuất. Nghĩa phải là nghĩa chính thức."""
        sense = self.get_object()
        if request.method == 'POST':
            if not sense.is_official:
                return _bad_request("Only official senses can be recommended")
            AISense.objects.filter(pk=sense.pk).update(recommended_by=request.user, recommended_at=timezone.now())
        else:
            AISense.objects.filter(pk=sense.pk).update(recommended_by=None, recommended_at=None)
        refresh_sense_scores(pk=sense.pk)
        clear_word_cache(sense)
        score = AISense.objects.filter(pk=sense.pk).values_list('score', flat=True).first()
        return Response({'recommended': request.method == 'POST', 'sense_id': str(sense.id), 'score': score})

    @action(detail=True, methods=['post'], url_path='promote', permission_classes=[IsAdminUser])
    def promote(self, request, pk=None):
        """
        Admin: biến nghĩa tuỳ chỉnh của user thành nghĩa chính thức của từ (nguồn bổ sung nghĩa tốt), tác giả được cộng uy tín.
        Body: replace (mặc định true nếu nghĩa tách từ 1 nghĩa chính thức): nghĩa gốc thôi chính thức, nghĩa mới thay chỗ.
        Nghĩa giữ original_id nên tiến độ học của mọi người với nghĩa gốc vẫn tiếp tục. Trường user đã sửa chỉ giữ ngôn ngữ
        user viết, bản dịch cũ (đã lỗi thời) bị bỏ để get-word dịch bù.
        """
        sense = self.get_object()
        if sense.is_official:
            return _bad_request("Sense is already official")
        original = sense.original
        replace = request.data.get('replace', original is not None and original.is_official)
        with transaction.atomic():
            sense.contents = promoted_contents(sense)
            sense.delta = {}
            sense.is_official = True
            sense.is_frozen = True
            sense.quality_status = 'verified'
            sense.updated_by = request.user
            proposal = (sense.ai_review or {}).get('proposal')
            if proposal and proposal.get('status') == 'pending':
                sense.ai_review = {**sense.ai_review, 'proposal': {**proposal, 'status': 'approved', 'approved_by': request.user.pk}}
            sense.save()
            replaced = None
            if replace and original is not None and original.is_official:
                AISense.objects.filter(pk=original.pk).update(is_official=False, updated_at=timezone.now())
                replaced = str(original.pk)
            if sense.created_by_id and sense.created_by_id != request.user.id:
                record(sense.created_by, 'sense_promoted', 'sense', sense.pk, by=request.user)
        clear_word_cache(sense)
        refresh_forms(sense.word_id)
        return Response({'id': str(sense.id), 'is_official': True, 'replaced': replaced})

    @action(detail=False, methods=['get'], url_path='top')
    def top(self, request):
        """
        Nghĩa chính thức đứng đầu toàn hệ thống theo `by`: forgot (số lần chấm Again), forget_rate (tỉ lệ quên, cần
        >= TOP_MIN_REVIEWS lần chấm), learned (số người học), liked (lượt thích). Query: lang (bắt buộc), by, limit (<= 100),
        user_lang (kèm nội dung ngôn ngữ đó, không đẩy dịch bù). Cache TOP_CACHE_SECONDS.
        """
        params = request.query_params
        language_code, user_language_code, by = params.get('lang'), params.get('user_lang'), params.get('by', 'forgot')
        if not is_lang(language_code) or (user_language_code is not None and not is_lang(user_language_code)):
            return _bad_request("lang is required, user_lang must be a language code")
        if by not in TOP_SENSE_ORDERS:
            return _bad_request("by must be one of: %s" % ', '.join(TOP_SENSE_ORDERS))
        try:
            limit = min(max(int(params.get('limit', 20)), 1), TOP_MAX)
        except ValueError:
            return _bad_request("Invalid limit")

        key = f'top_senses:{language_code}:{user_language_code or ""}:{by}'
        rows = cache.get(key)
        if rows is None:
            rows = [top_sense_row(sense, language_code, user_language_code) for sense in top_senses(language_code, by)[:TOP_MAX]]
            cache.set(key, rows, TOP_CACHE_SECONDS)
        return Response({'by': by, 'lang': language_code, 'senses': rows[:limit]})

    PROPOSE_TEXT = (8, 500)

    @action(
        detail=False, methods=['post'], url_path='propose', permission_classes=[IsAuthenticated, NotBanned],
        throttle_classes=[UserRateThrottle, SenseProposeThrottle],
    )
    def propose(self, request):
        """
        Đề xuất nghĩa mới cho 1 từ: chỉ cần gõ định nghĩa. Cổng AI (utils/ai/sense_gate.py) quyết định:
          official  -> nghĩa chính thức ngay (Claude accept >= SENSE_PROPOSE_AUTO_CONFIDENCE VÀ Gemini accept, bản dịch ổn)
          duplicate -> đã có nghĩa tương tự, trả nghĩa đó (không tạo gì)
          rejected  -> từ chối kèm lý do ngắn (nội dung chèn lệnh / spam / xấu còn bị trừ uy tín)
          pending   -> chờ admin duyệt (Claude không chắc, hai lớp bất đồng, hoặc Gemini chưa kịp về khi Claude xong); tác giả vẫn thấy riêng
        Body: word_id, user_lang, definition (8-500 ký tự), pos (tuỳ chọn). Trả {result, sense?, reason?}. Chạy đồng bộ (vài giây).
        Lỗi: 400 (tham số), 403 account_required (khách) / user_banned, 404 từ chưa có, 429 (throttle), 503 ai_unavailable.
        """
        user = request.user
        word_id, user_language_code = request.data.get('word_id'), request.data.get('user_lang')
        definition = request.data.get('definition')
        pos = request.data.get('pos') or ''
        if not is_uuid(word_id) or not is_lang(user_language_code):
            return _bad_request("Invalid word_id or user_lang")
        if not isinstance(definition, str) or not isinstance(pos, str):
            return _bad_request("definition must be a string")
        # Emoji / ký hiệu hình không phải một phần của định nghĩa (và làm nhiễu bước kiểm tra AI): bỏ trước khi đếm độ dài
        definition = re.sub(r'[🀀-🫿☀-➿️‍]', '', definition)
        definition = re.sub(r'\s+', ' ', definition).strip()
        low, high = self.PROPOSE_TEXT
        if not low <= len(definition) <= high:
            return _bad_request("definition must be %d-%d characters" % (low, high))
        pos = re.sub(r'[^a-zA-Z ]', '', pos).strip().lower()[:20]

        from user.models import UserProfile
        tier = UserProfile.objects.filter(user=user).values_list('tier', flat=True).first()
        if tier in (None, 'guest') and not user.is_staff and not settings.SENSE_PROPOSE_ALLOW_GUEST:
            return Response({"detail": "Register an account to propose senses", "errorCode": "account_required"}, status=status.HTTP_403_FORBIDDEN)

        word = AIWord.objects.filter(id=word_id, status='COMPLETED').exclude(is_deleted=True).exclude(is_active=False).first()
        if not word:
            return Response({"message": "Word not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            result = async_to_sync(sense_gate.evaluate_proposal)(
                word.value, word.language_code, user_language_code, pos, existing_senses_of(word), definition,
            )
        except Exception:
            logger.exception('Cổng AI đề xuất nghĩa lỗi (từ %s)', word.value)
            return Response({"detail": "AI service unavailable, try again later", "errorCode": "ai_unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        decision, claude = result['decision'], result['claude']
        logger.info('Đề xuất nghĩa [%s] %s: %s %s (Claude %s %.2f, Gemini %s)', word.value, user.pk, decision, result['reasons'],
                    claude['verdict'], claude['confidence'], result['gemini_status'])

        if decision == 'reject':
            if result['abuse']:
                record(user, 'sense_abuse', 'word', word.id)
            code = claude.get('reason_code') or 'not_a_meaning'
            if result['abuse'] and code not in sense_gate.ABUSE_REASON_CODES:
                code = 'injection' if claude.get('injection_attempt') else 'spam'
            # `reason_code` cho app dịch ra ngôn ngữ người dùng; `reason` (tiếng Anh) chỉ để log / admin
            return Response({'result': 'rejected', 'reason': claude['reason'], 'reason_code': code, 'abuse': result['abuse']})

        if decision == 'duplicate':
            existing = duplicate_sense(result)
            data = AISenseSerializer(existing).data if existing else None
            if existing:
                data['contents'] = self._user_contents(existing, user_language_code)
            return Response({'result': 'duplicate', 'reason': claude['reason'], 'sense': data})

        sense = create_proposed_sense(user, word, result, definition, user_language_code)
        data = AISenseSerializer(sense).data
        data['contents'] = promoted_contents(sense)
        if decision == 'official':
            record(user, 'sense_accepted', 'sense', sense.id)
            return Response({'result': 'official', 'sense': data}, status=status.HTTP_201_CREATED)
        return Response({'result': 'pending', 'reasons': result['reasons'], 'sense': data}, status=status.HTTP_201_CREATED)

    ITEM_KINDS = ('synonyms', 'antonyms', 'relateds')

    @action(
        detail=False, methods=['post'], url_path='add-items', permission_classes=[IsAuthenticated, NotBanned],
        throttle_classes=[UserRateThrottle, SenseItemsThrottle],
    )
    def add_items(self, request):
        """
        Thêm từ vào danh sách đồng nghĩa / trái nghĩa / liên quan của 1 nghĩa. Chỉ thêm, không xoá. Không tạo từ mới:
        Claude Haiku và Gemini (utils/ai/items_gate.py) cùng kiểm tra từ có phải mục từ thật và đúng quan hệ với nghĩa này,
        chỉ từ cả hai accept mới được ghi thẳng vào metadata của nghĩa (dữ liệu chính thức, mọi người thấy).
        Body: sense_id, kind (synonyms | antonyms | relateds), words (<= SENSE_ITEMS_MAX_WORDS).
        Trả {added: [từ], rejected: [{word, reason}], existing: [từ đã có trong danh sách]}.
        Lỗi: 400, 403 account_required / user_banned, 404, 429, 503 ai_unavailable (một trong hai AI lỗi: không thêm gì).
        """
        user = request.user
        sense_id, kind, words = request.data.get('sense_id'), request.data.get('kind'), request.data.get('words')
        if not is_uuid(sense_id) or kind not in self.ITEM_KINDS or not isinstance(words, list) or not words:
            return _bad_request("sense_id, kind and words are required")
        if len(words) > settings.SENSE_ITEMS_MAX_WORDS:
            return _bad_request("At most %s words per request" % settings.SENSE_ITEMS_MAX_WORDS)

        from user.models import UserProfile
        tier = UserProfile.objects.filter(user=user).values_list('tier', flat=True).first()
        if tier in (None, 'guest') and not user.is_staff and not settings.SENSE_PROPOSE_ALLOW_GUEST:
            return Response({"detail": "Register an account to add items", "errorCode": "account_required"}, status=status.HTTP_403_FORBIDDEN)

        sense = self.get_queryset().filter(id=sense_id).first()
        if not sense:
            return Response({"message": "Sense not found"}, status=status.HTTP_404_NOT_FOUND)

        language_code = sense.language_code
        current = ((sense.metadata.advanced or {}).get(kind) or []) if sense.metadata else []
        have = {str(w).strip().lower() for w in current} | {(sense.word_value or '').lower()}
        existing, rejected, candidates = [], [], []
        for raw in dict.fromkeys(w for w in words if isinstance(w, str)):
            word = normalize_lookup(raw)
            problem = lookup_entry_problem(word, language_code) if is_lookup_word(word) else 'not_a_word'
            if problem:
                rejected.append({'word': word or raw, 'reason': problem})
            elif word.lower() in have:
                existing.append(word)
            elif word.lower() in {c.lower() for c in candidates}:
                continue  # trùng với từ khác trong lượt này (khác hoa thường)
            elif len(current) + len(candidates) >= settings.SENSE_ITEMS_MAX_TOTAL:
                rejected.append({'word': word, 'reason': 'list_full'})
            else:
                candidates.append(word)

        added = []
        if candidates:
            contents = promoted_contents(sense)
            definition = (contents.get('definition') or {}).get(language_code, {}).get('value', '')
            examples = contents.get('examples') or [{}]
            example = ((examples[0] or {}).get(language_code) or {}).get('value', '')
            try:
                verdicts = async_to_sync(items_gate.evaluate_items)(
                    sense.word_value, language_code, sense.pos, definition, example, kind, candidates, [str(w) for w in current],
                )
            except Exception:
                logger.exception('Cổng AI danh sách lỗi (nghĩa %s)', sense.id)
                return Response({"detail": "AI service unavailable, try again later", "errorCode": "ai_unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            logger.info('Thêm vào %s của nghĩa %s: %s seconds=%s', kind, sense.id,
                        {c: (verdicts[c]['claude'], verdicts[c]['gemini']) for c in candidates}, verdicts['_seconds'])
            for word in candidates:
                if verdicts[word]['ok']:
                    added.append(word)
                else:
                    rejected.append({'word': word, 'reason': verdicts[word]['reason'] or 'rejected'})

        if added:
            with transaction.atomic():
                metadata = AISenseMetadata.objects.select_for_update().get(pk=sense.metadata_id) if sense.metadata_id else None
                if metadata is None:
                    metadata = AISenseMetadata.objects.create(created_by=user)
                    sense.metadata = metadata
                    sense.save(update_fields=['metadata', 'updated_at'])
                advanced = dict(metadata.advanced or {})
                merged = list(advanced.get(kind) or [])
                seen = {str(m).strip().lower() for m in merged}
                merged += [w for w in added if w.lower() not in seen]
                advanced[kind] = merged
                metadata.advanced = advanced
                metadata.save(update_fields=['advanced', 'updated_at'])
            clear_word_cache(sense)
        return Response({'added': added, 'rejected': rejected, 'existing': existing})

    @action(detail=False, methods=['post'], url_path='form-example', permission_classes=[IsAuthenticated, NotBanned])
    def form_example(self, request):
        """
        Ví dụ 1 câu riêng của 1 dạng biến cách (buying) dưới nghĩa của từ gốc (buy). Có sẵn thì trả ngay; chưa có thì Claude Haiku
        viết 1 câu, chỉ lưu khi câu chứa đúng dạng đó (ai/form_examples.py), dùng chung cho mọi người. Mỗi người tối đa
        FORM_EXAMPLES_PER_DAY lần tạo mới / ngày.
        Body: sense_id, form, user_lang. Trả {form, text, translate, created}.
        Lỗi: 400 (kể cả dạng không thuộc từ này), 403 account_required, 404, 422 no_example (AI không viết được câu hợp lệ),
        429 (hết lượt trong ngày), 503 ai_unavailable.
        """
        sense_id, form, user_lang = request.data.get('sense_id'), request.data.get('form'), request.data.get('user_lang')
        if not is_uuid(sense_id) or not isinstance(form, str) or not form.strip() or len(form) > 100 or not is_lang(user_lang):
            return _bad_request("sense_id, form and user_lang are required")
        sense = self.get_queryset().filter(id=sense_id).first()
        if not sense:
            return Response({"message": "Sense not found"}, status=status.HTTP_404_NOT_FOUND)
        form = normalize_lookup(form).casefold()
        if not form_examples.form_is_known(sense, form):
            return _bad_request("Not a form of this word")

        def reply(row, created):
            return Response({
                'form': row.form, 'text': row.text, 'created': created,
                'translate': (row.translations or {}).get(user_lang) if user_lang != row.language_code else None,
            })

        row = SenseFormExample.objects.filter(sense=sense, form=form).first()
        if row and (user_lang == row.language_code or user_lang in (row.translations or {})):
            return reply(row, False)

        from user.models import UserProfile
        tier = UserProfile.objects.filter(user=request.user).values_list('tier', flat=True).first()
        if tier in (None, 'guest') and not request.user.is_staff and not settings.SENSE_PROPOSE_ALLOW_GUEST:
            return Response({"detail": "Register an account to add examples", "errorCode": "account_required"}, status=status.HTTP_403_FORBIDDEN)
        key = 'form_example:%s:%s' % (request.user.pk, timezone.localdate())
        used = cache.get(key, 0)
        if used >= settings.FORM_EXAMPLES_PER_DAY:
            return Response({"detail": "Daily limit reached", "errorCode": "daily_limit"}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        cache.set(key, used + 1, 86400)

        contents = promoted_contents(sense)
        definition = (contents.get('definition') or {}).get(sense.language_code, {}).get('value', '')
        try:
            row, created = form_examples.get_form_example(sense, form, definition, user_lang)
        except Exception:
            logger.exception('Tạo ví dụ dạng lỗi (nghĩa %s, dạng %s)', sense.id, form)
            return Response({"detail": "AI service unavailable, try again later", "errorCode": "ai_unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if row is None:
            return Response({"detail": "No valid example", "errorCode": "no_example"}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        return reply(row, created)

    @action(detail=False, methods=['get'], url_path='proposals', permission_classes=[IsAdminUser])
    def proposals(self, request):
        """
        Admin: hàng chờ duyệt nghĩa người dùng đề xuất (status=pending mặc định | approved), mới nhất trước, <= 100.
        Duyệt bằng POST ai-senses/{id}/promote/ (thành chính thức), từ chối bằng DELETE ai-senses/{id}/.
        """
        wanted = request.query_params.get('status', 'pending')
        if wanted not in ('pending', 'approved'):
            return _bad_request("status must be pending or approved")
        try:
            limit = min(max(int(request.query_params.get('limit', 50)), 1), 100)
        except ValueError:
            return _bad_request("Invalid limit")
        senses = (
            AISense.objects.filter(ai_review__proposal__status=wanted).exclude(is_deleted=True)
            .select_related('metadata', 'created_by').order_by('-id')[:limit]
        )
        rows = []
        for sense in senses:
            rows.append({
                'id': str(sense.id), 'word_id': str(sense.word_id), 'word_value': sense.word_value, 'pos': sense.pos, 'level': sense.level,
                'language_code': sense.language_code, 'contents': promoted_contents(sense), 'is_official': sense.is_official,
                'proposal': sense.ai_review.get('proposal'), 'author': sense.created_by.username if sense.created_by_id else None,
                'created_at': sense.created_at,
            })
        return Response({'status': wanted, 'senses': rows})

    @action(detail=False, methods=['post'], url_path='translate', permission_classes=[IsAuthenticated])
    def translate(self, request):
        """
        Dịch trước cho 1 cụm nghĩa sắp học (app gọi khi chọn cụm 5-8 thẻ). Body: sense_ids (tối đa 30), user_lang.
        Nghĩa đã đủ bản dịch bỏ qua; thiếu thì đẩy việc dịch (nghĩa user sửa thì dịch phần delta). Trả {ready, pending}: id nghĩa.
        App hỏi lại sau ít giây (pending giảm dần) hoặc tải lại bundle.
        """
        from ai.translation import collect_gaps, missing_sense_ids, queue_gaps
        from ai.queries import visible_senses

        sense_ids, user_language_code = request.data.get('sense_ids'), request.data.get('user_lang')
        if not isinstance(sense_ids, list) or not 0 < len(sense_ids) <= 30 or not all(is_uuid(i) for i in sense_ids) or not is_lang(user_language_code):
            return Response({'message': 'sense_ids (1-30) and user_lang are required'}, status=status.HTTP_400_BAD_REQUEST)
        senses = list(visible_senses(request.user).filter(id__in=sense_ids).select_related('word', 'original'))
        gaps = collect_gaps(senses, user_language_code)
        queue_gaps(gaps, user_language_code)
        pending = set(missing_sense_ids(gaps))
        return Response({'ready': [str(s.id) for s in senses if str(s.id) not in pending], 'pending': sorted(pending)})

    @action(detail=False, methods=['get'], url_path='community', permission_classes=[AllowAny])
    def community(self, request):
        """
        Gợi ý nghĩa của cộng đồng cho 1 từ (để điền nhanh form thêm / sửa nghĩa), xếp theo `rank` (xem community_senses).
        Query: word_id (bắt buộc), user_lang (kèm nội dung ngôn ngữ đó, không đẩy dịch bù), ordering (popular mặc định | newest),
        limit (<= 50, mặc định 10). Có token thì bỏ nghĩa của mình / đã ẩn và báo `liked`. Chỉ nghĩa đã phát hành mới có mặt,
        chưa ai phát hành thì `senses` rỗng.
        """
        params = request.query_params
        word_id, user_language_code = params.get('word_id'), params.get('user_lang')
        ordering = params.get('ordering', 'popular')
        if not is_uuid(word_id):
            return _bad_request("word_id is required")
        if user_language_code is not None and not is_lang(user_language_code):
            return _bad_request("user_lang must be a language code")
        if ordering not in COMMUNITY_ORDERS:
            return _bad_request("ordering must be one of: %s" % ', '.join(COMMUNITY_ORDERS))
        try:
            limit = min(max(int(params.get('limit', 10)), 1), COMMUNITY_MAX)
        except ValueError:
            return _bad_request("Invalid limit")

        word = AIWord.objects.filter(id=word_id).exclude(is_deleted=True).first()
        if not word:
            return Response({"message": "Word not found"}, status=status.HTTP_404_NOT_FOUND)

        queryset = community_senses(word.id, request.user)
        queryset = queryset.order_by('-rank', '-id') if ordering == 'popular' else queryset.order_by('-id')
        senses = list(queryset[:limit])

        liked = set()
        if request.user.is_authenticated and senses:
            liked = {
                str(target) for target in UserInteraction.objects.filter(
                    user=request.user, target_type='sense', interaction_type='like', status=True, target_id__in=[s.id for s in senses],
                ).values_list('target_id', flat=True)
            }
        langs = {word.language_code, user_language_code} - {None}
        return Response({'word_id': str(word.id), 'ordering': ordering, 'senses': [community_row(sense, langs, liked) for sense in senses]})

    @action(detail=False, methods=['get'], url_path='mine', permission_classes=[IsAuthenticated])
    def mine(self, request):
        """Nghĩa tuỳ chỉnh của mình (lọc theo word_id nếu có), id các nghĩa đang ẩn và đang thích. Tối đa 500 nghĩa mới nhất."""
        word_id = request.query_params.get('word_id')
        if word_id is not None and not is_uuid(word_id):
            return _bad_request("Invalid word_id")
        senses = AISense.objects.filter(created_by=request.user, is_official=False).exclude(is_deleted=True).exclude(is_active=False)
        mine = UserInteraction.objects.filter(user=request.user, target_type='sense', status=True)
        hidden, liked = mine.filter(interaction_type='hide'), mine.filter(interaction_type='like')
        if word_id:
            senses = senses.filter(word_id=word_id)
            word_senses = AISense.objects.filter(word_id=word_id).values('id')
            hidden, liked = hidden.filter(target_id__in=word_senses), liked.filter(target_id__in=word_senses)
        senses = senses.select_related('metadata').order_by('-id')[:500]
        return Response({
            'senses': AISenseSerializer(senses, many=True).data,
            'hidden': [str(target) for target in hidden.values_list('target_id', flat=True)],
            'liked': [str(target) for target in liked.values_list('target_id', flat=True)],
        })

    def _user_contents(self, sense, user_language_code):
        """
        Nội dung nghĩa theo ngôn ngữ của user; thiếu bản dịch thì đẩy job dịch bù cho worker.
        Nghĩa tuỳ chỉnh dạng delta không có contents riêng mà dùng contents của nghĩa gốc, nên bản dịch bù
        cũng phải ghi vào nghĩa gốc (job trỏ tới nghĩa gốc).
        """
        from ai.translation import queue_gaps, sense_gaps

        target = sense if sense.contents else sense.original
        if target is None:
            return {}
        contents, missing = get_user_lang_sense(sense.language_code, user_language_code, target.contents, str(target.id))
        if missing:
            queue_translation(sense.word_id, sense.language_code, user_language_code, [missing], [AISenseSerializer(target).data])
        # Phần user đã sửa trong delta (ngôn ngữ họ viết) dịch riêng, ghi vào delta của nghĩa riêng
        queue_gaps([gap for gap in sense_gaps(sense, user_language_code) if gap.delta], user_language_code)
        return contents

    def retrieve(self, request, *args, **kwargs):
        sense = self.get_object()
        user_language_code = request.query_params.get('user_lang', 'en')
        if not is_lang(user_language_code):
            return _bad_request("Invalid user_lang")

        data = AISenseSerializer(sense).data
        data['contents'] = self._user_contents(sense, user_language_code)
        return Response(data)

    @action(detail=False, methods=['post'], url_path='create-sense', permission_classes=[IsAdminUser])
    def create_sense(self, request, *args, **kwargs):
        """
        Chỉ admin (dữ liệu mẫu, nhập tay): tạo nghĩa riêng không qua cổng AI. Người dùng KHÔNG thêm nghĩa tự do nữa: họ gửi đề xuất qua
        ai-senses/propose, cổng AI quyết định official / pending / duplicate / rejected.
        params: word_id, user_lang, contents{definition{<lang>{value}}, usage{}, examples[{}], translations{}}, metadata
        """
        user = request.user
        word_id = request.data.get('word_id')
        user_language_code = request.data.get('user_lang')
        raw_metadata = request.data.get('metadata')

        if not word_id or not user_language_code or not request.data.get('contents') or not isinstance(raw_metadata, dict):
            return _bad_request("Missing required parameters")
        if not is_uuid(word_id) or not is_lang(user_language_code):
            return _bad_request("Invalid word_id or user_lang")

        word_instance = AIWord.objects.filter(id=word_id).exclude(is_deleted=True).first()
        if not word_instance:
            return Response({"message": "Word not found"}, status=status.HTTP_404_NOT_FOUND)

        contents = clean_contents(request.data.get('contents'))
        if not contents.get('definition', {}).get(word_instance.language_code):
            return _bad_request("Missing definition")
        audio, error = clean_media_param(user, request.data.get('audio'), 'audio')
        image, image_error = clean_media_param(user, request.data.get('image'), 'image')
        if error or image_error:
            return _bad_request(error or image_error)

        with transaction.atomic():
            metadata = AISenseMetadata.objects.create(**clean_metadata(raw_metadata), created_by=user)
            sense_instance = AISense.objects.create(
                word=word_instance, word_value=word_instance.value, metadata=metadata,
                is_frozen=None, contents=contents, created_by=user, audio=audio or None, image_preview=image or None,
                language_code=word_instance.language_code, is_official=False, is_ai_created=False,
            )

        clear_word_cache(sense_instance)
        data = AISenseSerializer(sense_instance).data
        _, missing = get_user_lang_sense(word_instance.language_code, user_language_code, contents, str(sense_instance.id))
        if missing:
            # Phần còn lại dịch xong sẽ về qua socket / lần tra sau
            queue_translation(word_instance.id, word_instance.language_code, user_language_code, [missing], [data])
            return Response({'detail': 'Word incomplete.', 'status': '206', 'data': data}, status=status.HTTP_206_PARTIAL_CONTENT)

        return Response({'detail': 'Sense created.', 'status': '201', 'data': data}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['put'], url_path='update-sense', permission_classes=[IsAuthenticated])
    def update_sense(self, request, *args, **kwargs):
        """
        Sửa nghĩa bằng delta. Nghĩa chính thức, nghĩa đã đóng băng hoặc nghĩa của người khác không bị sửa tại chỗ
        mà tách ra thành một bản tuỳ chỉnh mới của user; chỉ nghĩa tuỳ chỉnh của chính user (chưa đóng băng) mới sửa tại chỗ.
        """
        user = request.user
        sense_id = request.data.get('sense_id')
        user_language_code = request.data.get('user_lang')
        delta = request.data.get('delta') or {}
        raw_metadata = request.data.get('metadata')

        if not is_uuid(sense_id) or not is_lang(user_language_code):
            return _bad_request("Invalid sense_id or user_lang")
        if not isinstance(delta, dict) or len(json.dumps(delta)) > MAX_DELTA_CHARS:
            return _bad_request("Invalid delta")
        image, error = clean_media_param(user, request.data.get('image'), 'image')
        audio, audio_error = clean_media_param(user, request.data.get('audio'), 'audio')
        if error or audio_error:
            return _bad_request(error or audio_error)

        sense = self.get_queryset().filter(id=sense_id).first()
        if not sense:
            return Response({"message": "Sense not found"}, status=status.HTTP_404_NOT_FOUND)
        sense = apply_sense_edit(user, sense, delta, raw_metadata, image, audio)

        data = AISenseSerializer(sense).data
        data['contents'] = self._user_contents(sense, user_language_code)
        return Response(data, status=status.HTTP_200_OK)
