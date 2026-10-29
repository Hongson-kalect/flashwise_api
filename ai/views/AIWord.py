import logging
import time
from urllib.parse import unquote

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from ai.models.AISense import AISense
from ai.models.AISenseContent import AISenseContent
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AIWord import AIWord
from ai.models.TranslateLog import TranslateLog
from ai.generation import GenerationError, start_word_generation
from ai.jobs import queue_translation
from ai.queries import load_word
from ai.serializers.AIWord import AIWordSerializer
from core.models import Collection, CollectionItem, ImageContext, ImageLibrary, ImageLibraryContext
from utils.redis.word_init import WordCacheManager
from utils.utils.permissions import IsAdminOrReadOnly, WordGenerateThrottle, get_request_user
from utils.utils.sense_handle import get_user_lang_content
from utils.utils.soft_delete_viewset import SoftDeleteViewSet

logger = logging.getLogger(__name__)

def detect_missing_content(senses, language_code, user_language_code):
    # Kết quả trả về: { "sense_id": { "definition": "...", "examples": [...] } }
    missing_data = []
    # Danh sách các Sense cần dịch nghĩa tổng quát (translations field)

    for s in senses:
        sid = str(s.id)
        c_json = s.contents or {}
        sense_missing = {}

        # --- 1. Kiểm tra Bản dịch tổng quát (field: translations) ---
        trans_node = c_json.get('translations', {})
        if not trans_node.get(user_language_code):
            # Nếu thiếu bản dịch tổng quát, lấy definition làm gốc để AI dịch
            def_node = c_json.get('definition', {})
            sense_missing['translations'] = def_node.get(language_code, True)
        else :
            sense_missing['translations'] = False

        # --- 2. Duyệt qua các thành phần bên trong contents ---
        for c_type, j in c_json.items():
            if c_type in ['translations', 'collocations', 'idioms', 'metadata']:
                continue

            # Xử lý Dictionary (definition, usage, pronunciation...)
            if isinstance(j, dict):
                orig_val = j.get(language_code)
                if orig_val and not j.get(user_language_code):
                    sense_missing.setdefault(c_type,{})[language_code] = orig_val

            # Xử lý List (examples)
            elif isinstance(j, list) and c_type == 'examples':
                ex_missing = []
                for index, ex in enumerate(j):
                    ex_orig = ex.get(language_code)
                    if ex_orig and not ex.get(user_language_code):
                        ex_missing.append({'index': index, language_code:{'value': ex_orig}})

                if ex_missing:
                    sense_missing['examples'] = ex_missing

        # Nếu chỉ có Translate = False thì là ko cần dịch
        if sense_missing['translations'] ==False and len(list(sense_missing.keys()))>1:
            missing_data.append({"id": str(s.id),"contents":{**sense_missing}})

    return missing_data


def _word_response(detail, http_status, data=None):
    return Response({'detail': detail, 'status': str(http_status), 'data': data}, status=http_status)


def _is_stale(started_at, max_age=None):
    """Job tạo từ chạy quá lâu mà vẫn PROCESSING: worker nhiều khả năng đã chết."""
    if not started_at:
        return False
    return time.time() - started_at > (max_age or settings.WORD_PROCESSING_STALE_SECONDS)


def _respond_from_cache(cache_manager, cached, word, language_code, user_language_code):
    state = cached.get('status')
    cached_word = cached.get('word') or {}
    senses = cached.get('senses') or {}

    if state == 'CACHED':
        data = {**cached_word, "senses": senses}

        # Đường nhanh: mọi nghĩa đã có đủ bản dịch cho ngôn ngữ này -> không cần dò thiếu nội dung
        if user_language_code in (cached.get('languages') or []):
            return _word_response('CACHED', 200, data)

        _, missing, current_senses = get_user_lang_content(
            language_code, user_language_code, {**cached_word, "senses": list(senses.values())}
        )
        if missing:
            queue_translation(cached_word.get('id'), language_code, user_language_code, missing, current_senses)
            return _word_response('CACHED', 206, data)
        return _word_response('CACHED', 200, data)

    if state == 'ERROR':
        return _word_response('ERROR', 500, cached)

    if state == 'NOT_FOUND':
        return _word_response('NOT_FOUND', 404, cached)

    # PROCESSING (và các trạng thái trung gian): người tra sau vào chờ chung, nhớ đăng ký ngôn ngữ của họ để job đang chạy dịch luôn
    if user_language_code not in (cached.get('langs') or []):
        cache_manager.cache_word_add_translate(language_code, word, user_language_code)
    return _word_response('PROCESSING', 202, cached)


def _respond_completed(cache_manager, word_instance, word, language_code, user_language_code):
    data = AIWordSerializer(word_instance).data
    senses, missing, current_senses = get_user_lang_content(language_code, user_language_code, data)
    senses_by_id = {sense.get('id'): sense for sense in senses}
    body = {**data, "senses": senses_by_id}

    if missing:
        # Trả phần đã có ngay, phần thiếu dịch xong sẽ về qua socket
        queue_translation(word_instance.id, language_code, user_language_code, missing, current_senses)
        return _word_response('Word incomplete.', 206, body)

    cache_manager.cache_word(language_code, word_instance.id, word, senses_by_id)
    return _word_response('DATABASE_DATA', 200, body)


def _join_processing(cache_manager, word_instance, word, language_code, user_language_code):
    """DB nói đang tạo nhưng cache đã hết hạn: dựng lại cache (đúng id trong DB) để người tra này chờ qua socket."""
    created, init_data = cache_manager.cache_word_init(language_code, word, user_language_code)
    if created:
        init_data['word']['id'] = str(word_instance.id)
        cache_manager.cache_word_set_word(language_code, word, init_data['word'])
    elif user_language_code not in (init_data.get('langs') or []):
        cache_manager.cache_word_add_translate(language_code, word, user_language_code)
    return _word_response('PROCESSING', 202, init_data)


def _start_generation(request, cache_manager, word, language_code, user_language_code, existing):
    """Từ chưa có (hoặc lần tạo trước lỗi / treo): đẩy job tạo từ cho worker. existing là bản ghi AIWord cũ nếu có."""
    try:
        # Mỗi từ mới là 2 lần gọi AI (draft + refine) cộng dịch / metadata, nên giới hạn theo người dùng / IP
        state, init_data = start_word_generation(
            cache_manager, word, language_code, user_language_code, lambda: get_request_user(request), existing,
            allow=lambda: WordGenerateThrottle().allow_request(request, None),
        )
    except GenerationError:
        return Response({'detail': 'Service unavailable, try again later'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    if state == 'joined':  # có người khởi tạo trước, vào chờ chung
        return _word_response('PROCESSING', 202, init_data)
    if state == 'throttled':
        return Response(
            {'detail': 'Too many new words, try again later', 'errorCode': 'word_generate_throttled'},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    return _word_response('PROCESSING', 201, {**init_data['word'], "senses": init_data.get('senses')})


class AIWordViewSet(SoftDeleteViewSet):
    queryset = AIWord.objects.all()
    serializer_class = AIWordSerializer
    # Tra từ không cần đăng nhập (có throttle), sửa / xoá dữ liệu từ điển chỉ admin
    permission_classes = [IsAdminOrReadOnly]

    @action(detail=False, methods=['get'], url_path='get-word')
    def get_word(self, request, *args, **kwargs):
        """
        params: value, lang, user_lang. App kết nối socket theo md5(word)+lang để nhận nghĩa khi đang tạo.

        Thứ tự: cache Redis -> DB -> tạo mới bằng AI (draft rồi refine, xem utils/ai/word_render.py).
        200 đủ dữ liệu, 206 thiếu bản dịch (đang dịch bù), 201 vừa nhận việc tạo từ, 202 đang tạo,
        404 từ không hợp lệ, 429 tạo từ mới quá nhiều, 500 lần tạo lỗi.
        """
        raw_word = request.GET.get('value')
        language_code = request.GET.get('lang')
        user_language_code = request.GET.get('user_lang')
        if not raw_word or not language_code or not user_language_code:
            return Response({'detail': 'value, lang, user_lang are required'}, status=status.HTTP_400_BAD_REQUEST)

        word = unquote(raw_word).strip().lower()
        if not word or len(word) > 100 or len(language_code) > 10 or len(user_language_code) > 10:
            return Response({'detail': 'Invalid word or language code'}, status=status.HTTP_400_BAD_REQUEST)

        cache_manager = WordCacheManager()
        cached = cache_manager.cache_word_get_data(language_code, word)

        if cached and cached.get('status') == 'PROCESSING' and _is_stale(cached.get('started_at')):
            # Key bị đọc liên tục nên TTL không tự hết; job chết thì bỏ key để đi tiếp xuống DB và tạo lại
            cache_manager.cache_word_clear_specific(language_code, word)
            cached = None

        if cached:
            return _respond_from_cache(cache_manager, cached, word, language_code, user_language_code)

        word_instance = load_word(value=word, language_code=language_code)

        if word_instance is not None:
            state = word_instance.status
            if state == 'REJECTED':
                return _word_response('NOT_FOUND', 404, {'value': word, 'language_code': language_code})
            if state == 'COMPLETED':
                return _respond_completed(cache_manager, word_instance, word, language_code, user_language_code)
            if state != 'FAILED':
                updated_at = word_instance.updated_at or word_instance.created_at
                stale = updated_at is None or (timezone.now() - updated_at).total_seconds() > settings.WORD_PROCESSING_STALE_SECONDS
                if not stale:
                    return _join_processing(cache_manager, word_instance, word, language_code, user_language_code)

        return _start_generation(request, cache_manager, word, language_code, user_language_code, word_instance)

    @action(detail=False, methods=['delete'], url_path='clear', permission_classes=[IsAdminUser])
    def clear(self, request):
        try:
            # Xoá toàn bộ dữ liệu trên các bảng chỉ định
            # Lưu ý: .all().delete() sẽ kích hoạt các tín hiệu (signals)
            # Nếu muốn xoá cực nhanh và không cần signals, dùng ._raw_delete()
            cache = WordCacheManager()
            AISenseMetadata.objects.all().delete()
            ImageLibraryContext.objects.all().delete()
            ImageLibrary.objects.all().delete()
            ImageContext.objects.all().delete()
            CollectionItem.objects.all().delete()
            AISenseContent.objects.all().delete()
            AISense.objects.all().delete()
            AIWord.objects.all().delete()
            TranslateLog.objects.all().delete()
            Collection.objects.all().delete()

            cache.cache_word_clear_all()

            return Response(
                {"message": "Đã xoá sạch dữ liệu trên các bảng được chỉ định!"},
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def get_queryset(self):
        # Chỉ lấy các Word đang hoạt động
        return super().get_queryset().filter(is_active=True)
