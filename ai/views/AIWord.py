import logging
import time
from urllib.parse import unquote

from django.conf import settings
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import F, Prefetch
from django.db.models.functions import Length
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from ai.models.AISense import AISense
from ai.models.AISenseContent import AISenseContent
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AIWord import AIWord
from ai.models.TranslateLog import TranslateLog
from ai.bulk import apply_form_of, clean_words, describe_words, fold_forms, ranked_official_senses, sense_brief, sort_words, start_words
from ai.generation import GenerationError, start_word_generation
from ai.jobs import queue_translation
from ai.forms import form_of_guess_payload, form_of_payload, forms_of, guess_lemmas, resolve_form
from ai.models import WordForm
from ai.queries import load_word, official_senses, personalize_word
from ai.serializers.AIWord import AIWordSerializer
from core.models import Collection, CollectionItem, ImageContext, ImageLibrary, ImageLibraryContext
from utils.redis.word_init import WordCacheManager
from utils.utils.normalize import is_lookup_word, lookup_entry_problem, normalize_lookup
from ai.validators import is_lang, is_uuid
from utils.utils.permissions import IsAdminOrReadOnly, WordGenerateThrottle, consume_daily_quota, get_request_user
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


def _backfill_images(response):
    """Từ đã hoàn thành mà còn nghĩa chưa có ảnh thì nhờ worker vẽ bù (utils/image_gen/service.request_missing_images). Không bao giờ làm hỏng phản hồi."""
    try:
        data = response.data.get('data') if response.status_code in (200, 206) and isinstance(response.data, dict) else None
        if isinstance(data, dict) and data.get('id'):
            from utils.image_gen.service import request_missing_images
            request_missing_images(data['id'], data)
    except Exception:
        logger.exception('Không xếp được việc vẽ ảnh bù')
    return response


def _personal(request, response):
    """Dữ liệu từ dùng chung (cache / DB) + nghĩa riêng của user đang đăng nhập, ghép lúc trả về nên cache vẫn dùng chung."""
    data = response.data.get('data') if isinstance(response.data, dict) else None
    if response.status_code in (200, 206) and isinstance(data, dict) and isinstance(data.get('senses'), dict):
        response.data['data'] = personalize_word(data, request.user)
    return response


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
        # Không trả nguyên cache (nội bộ); lỗi được thử lại sau WORD_ERROR_RETRY_SECONDS (xem get_word)
        return _word_response('ERROR', 500, {'value': word, 'language_code': language_code, 'retry_after': settings.WORD_ERROR_RETRY_SECONDS})

    if state == 'NOT_FOUND':
        return _word_response('NOT_FOUND', 404, {'value': word, 'language_code': language_code})

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

    cache_manager.cache_word(language_code, word_instance.id, word, senses_by_id, word_fields=data)
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
    problem = lookup_entry_problem(word, language_code)
    if problem:  # câu bất kỳ không phải mục từ: từ chối ngay, không tốn lượt AI (từ đã có trong DB vẫn được trả ở các nhánh trước)
        return Response(
            {'detail': 'Only words, phrases, idioms and proverbs can be looked up', 'errorCode': 'not_a_dictionary_entry', 'reason': problem},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        # Mỗi từ mới là 2 lần gọi AI (draft + refine) cộng dịch / metadata, nên giới hạn theo người dùng / IP
        # Từ còn nghĩa chính thức (lần tạo lại trước bị treo / lỗi giữa chừng): bản mới thay hẳn, không cộng dồn
        old_ids = [str(s.id) for s in existing.senses.all()] if existing is not None else []
        state, init_data = start_word_generation(
            cache_manager, word, language_code, user_language_code, lambda: get_request_user(request), existing,
            allow=lambda: WordGenerateThrottle().allow_request(request, None),
            extra={'replace_sense_ids': old_ids} if old_ids else None,
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


MAX_CHECK_WORDS = 20


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
        200 với detail FORM_OF: chuỗi là dạng biến cách (ran, killed) của từ gốc chưa có trong DB, data = {redirect: từ gốc, form_of}: app tra tiếp từ gốc,
        404 từ không hợp lệ, 429 tạo từ mới quá nhiều, 500 lần tạo lỗi.
        """
        raw_word = request.GET.get('value')
        language_code = request.GET.get('lang')
        user_language_code = request.GET.get('user_lang')
        if not raw_word or not language_code or not user_language_code:
            return Response({'detail': 'value, lang, user_lang are required'}, status=status.HTTP_400_BAD_REQUEST)

        word = normalize_lookup(unquote(raw_word))  # cùng cách chuẩn hoá với worker và room socket của app
        if not is_lookup_word(word) or len(language_code) > 10 or len(user_language_code) > 10:
            return Response({'detail': 'Invalid word or language code'}, status=status.HTTP_400_BAD_REQUEST)

        cache_manager = WordCacheManager()
        cached = cache_manager.cache_word_get_data(language_code, word)

        if cached and cached.get('status') == 'PROCESSING' and _is_stale(cached.get('started_at')):
            # Key bị đọc liên tục nên TTL không tự hết; job chết thì bỏ key để đi tiếp xuống DB và tạo lại
            cache_manager.cache_word_clear_specific(language_code, word)
            cached = None
        elif cached and cached.get('status') == 'ERROR' and time.time() - (cached.get('finished_at') or 0) > settings.WORD_ERROR_RETRY_SECONDS:
            # Tương tự: mỗi lần đọc lại gia hạn TTL nên lỗi tạm thời (API AI chập chờn) sẽ trả 500 mãi; quá hạn thì xuống DB (FAILED) để tạo lại
            cache_manager.cache_word_clear_specific(language_code, word)
            cached = None

        if cached:
            return _backfill_images(_personal(request, _respond_from_cache(cache_manager, cached, word, language_code, user_language_code)))

        word_instance = load_word(value=word, language_code=language_code)

        if word_instance is None or word_instance.status in ('REJECTED', 'BANNED'):
            # Chưa có mục riêng (hoặc mục riêng bị từ chối, vd `comes` chỉ có nghĩa lạ): nếu là dạng biến cách (số nhiều, quá khứ...) của từ đã có thì trả từ gốc kèm form_of
            candidates = resolve_form(word, language_code)
            if candidates:
                lemma = load_word(id=candidates[0].word.id)
                response = _respond_completed(cache_manager, lemma, lemma.value, language_code, user_language_code)
                if isinstance(response.data.get('data'), dict):
                    response.data['data']['form_of'] = form_of_payload(word, candidates, own_entry=False)
                return _personal(request, response)
            # Dạng biến cách mà từ gốc chưa có trong DB: không tạo mục riêng cho dạng này; trả `redirect` để app tra từ gốc (get-word của từ gốc
            # tạo online như mọi từ mới) và giữ `form_of` để hiện "là dạng ... của X" và ví dụ riêng của dạng đã tra
            guesses = guess_lemmas(word, language_code)
            if guesses:
                return _word_response('FORM_OF', 200, {'redirect': guesses[0].lemma, 'form_of': form_of_guess_payload(word, guesses)})

        if word_instance is not None:
            state = word_instance.status
            if state in ('REJECTED', 'BANNED'):
                return _word_response('NOT_FOUND', 404, {'value': word, 'language_code': language_code})
            if state == 'COMPLETED':
                return _backfill_images(_personal(request, _respond_completed(cache_manager, word_instance, word, language_code, user_language_code)))
            if state != 'FAILED':
                updated_at = word_instance.updated_at or word_instance.created_at
                stale = updated_at is None or (timezone.now() - updated_at).total_seconds() > settings.WORD_PROCESSING_STALE_SECONDS
                if not stale:
                    return _join_processing(cache_manager, word_instance, word, language_code, user_language_code)

        return _start_generation(request, cache_manager, word, language_code, user_language_code, word_instance)

    @action(detail=False, methods=['get'], url_path='forms', permission_classes=[AllowAny])
    def forms(self, request):
        """
        Dạng biến cách của 1 từ cho phần "Biến thể" của trang từ. Query: word_id.
        Trả {forms: [{form, pos, tag, irregular}], also_form_of: {form, lemma, pos, tag, own_entry, candidates} | null}:
        also_form_of có khi từ này cũng là dạng biến cách của từ khác (saw: quá khứ của see). Tách khỏi get-word để đường tra nóng
        không tốn thêm truy vấn; dạng chưa có thì dựng lười bằng thư viện biến cách (ai/forms.py).
        """
        word_id = request.GET.get('word_id')
        if not is_uuid(word_id):
            return Response({'detail': 'word_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        word = AIWord.objects.filter(id=word_id, status='COMPLETED').exclude(is_deleted=True).exclude(is_active=False).first()
        if word is None:
            return Response({'detail': 'Word not found'}, status=status.HTTP_404_NOT_FOUND)
        others = resolve_form(word.value, word.language_code, analyze=False)
        return Response({
            'forms': forms_of(word),
            'also_form_of': form_of_payload(word.value, others, own_entry=True) if others else None,
        })

    @action(detail=False, methods=['post'], url_path='bulk-create', permission_classes=[IsAuthenticated])
    def bulk_create(self, request):
        """
        Tạo nhiều từ cùng lúc (từ chưa có được AI tạo dần, không cần đăng nhập vào bộ nào). Body: {lang, user_lang, words}.
        Chỉ nhận từ đơn, cụm từ, thành ngữ, tục ngữ: câu bất kỳ vào invalid ngay, không tốn lượt AI. Từ mới tính vào hạn mức
        `bulk_words` theo ngày (429 bulk_quota_exceeded, không tạo từ nào). Bộ từ gọi lại các hàm trong ai/bulk.py.
        Trả {results: {từ chuẩn hoá: {status, reason?}}}: exists | processing | rejected | invalid | unknown.
        Hỏi lại tiến độ bằng ai-words/check (chỉ đọc) cho tới khi hết processing.
        """
        language_code, user_language_code, words = request.data.get('lang'), request.data.get('user_lang'), request.data.get('words')
        if not is_lang(language_code) or not is_lang(user_language_code) or not isinstance(words, list) or not words:
            return Response({'detail': 'lang, user_lang and words are required'}, status=status.HTTP_400_BAD_REQUEST)
        if len(words) > settings.BULK_MAX_WORDS_PER_REQUEST:
            return Response({'detail': 'At most %s words per request' % settings.BULK_MAX_WORDS_PER_REQUEST}, status=status.HTTP_400_BAD_REQUEST)

        folded, form_of = fold_forms(clean_words(words), language_code)  # dạng chia -> từ gốc (killed -> kill), không tạo mục riêng cho dạng chia
        found, invalid, pending, to_start, existing = sort_words(folded, language_code)
        if to_start and not consume_daily_quota(request.user, 'bulk_words', len(to_start), settings.BULK_WORDS_PER_DAY):
            return Response({'detail': 'Daily new-word limit reached', 'errorCode': 'bulk_quota_exceeded'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        started = start_words(request.user, to_start, existing, language_code, user_language_code)
        results = describe_words([*words, *folded], language_code, found, invalid, pending, to_start, existing, started)
        return Response({'results': apply_form_of(results, form_of)})

    @action(detail=False, methods=['post'], url_path='check', permission_classes=[IsAuthenticated])
    def check(self, request):
        """Tiến độ của nhiều từ (chỉ đọc, không tạo, không tính hạn mức). Body: {lang, words (≤ 20)}. Kết quả như bulk-create."""
        language_code, words = request.data.get('lang'), request.data.get('words')
        if not is_lang(language_code) or not isinstance(words, list) or not words:
            return Response({'detail': 'lang and words are required'}, status=status.HTTP_400_BAD_REQUEST)
        if len(words) > MAX_CHECK_WORDS:
            return Response({'detail': f'At most {MAX_CHECK_WORDS} words'}, status=status.HTTP_400_BAD_REQUEST)
        folded, form_of = fold_forms(clean_words(words), language_code)
        found, invalid, pending, to_start, existing = sort_words(folded, language_code)
        results = apply_form_of(describe_words([*words, *folded], language_code, found, invalid, pending, to_start, existing), form_of)
        # Dạng biến cách đã biết của từ có sẵn (bought -> buy) mở được ngay nên cũng là "đã có"; form_of cho app biết từ gốc
        unknown = [key for key, r in results.items() if r['status'] == 'unknown']
        if unknown:
            lemma_of = {}
            for row in WordForm.objects.filter(
                language_code=language_code, form__in=unknown, word__status='COMPLETED', word__is_deleted=False, word__is_active=True,
            ).exclude(word__value=F('form')).select_related('word'):
                lemma_of.setdefault(row.form, row.word.value)
            for key, lemma in lemma_of.items():
                results[key] = {'status': 'exists', 'form_of': lemma}
        return Response({'results': results})

    @action(detail=False, methods=['get'], url_path='senses', permission_classes=[IsAuthenticated])
    def senses(self, request):
        """
        Các nghĩa chính thức của nhiều từ để người dùng chọn khi thêm từ vào bộ (chế độ chuyên sâu). Chỉ đọc: không tạo từ,
        không tính hạn mức. Query: lang, words (ngăn cách bằng dấu phẩy, tối đa 20), user_lang (tuỳ chọn, ngôn ngữ của định nghĩa).
        Trả {results: {từ: {status, senses?}}}: status như ai-words/check; từ exists có senses [{id, pos, level, like_count,
        definition, translations, example, image_preview, best}] theo độ tốt giảm dần, `best` đánh dấu nghĩa chế độ nhanh sẽ chọn.
        """
        language_code = request.query_params.get('lang')
        user_language_code = request.query_params.get('user_lang') or language_code
        raw = [w for w in (request.query_params.get('words') or '').split(',') if w.strip()]
        if not is_lang(language_code) or not is_lang(user_language_code) or not raw:
            return Response({'detail': 'lang and words are required'}, status=status.HTTP_400_BAD_REQUEST)
        if len(raw) > MAX_CHECK_WORDS:
            return Response({'detail': f'At most {MAX_CHECK_WORDS} words'}, status=status.HTTP_400_BAD_REQUEST)
        folded, form_of = fold_forms(clean_words(raw), language_code)
        found, invalid, pending, to_start, existing = sort_words(folded, language_code)
        results = describe_words([*raw, *folded], language_code, found, invalid, pending, to_start, existing)
        by_word = {}
        for sense in ranked_official_senses(list(found), language_code):
            by_word.setdefault(sense.word_value, []).append(sense)
        for word, rows in by_word.items():
            briefs = [sense_brief(s, user_language_code) for s in rows]
            for index, brief in enumerate(briefs):
                brief['best'] = index == 0
            results[word]['senses'] = briefs
        for form, lemma in form_of.items():  # dạng chia dùng chung nghĩa của từ gốc
            results[form] = {**results.get(lemma, {'status': 'unknown'}), 'form_of': lemma}
        return Response({'results': results})

    @action(detail=False, methods=['get'], url_path='search')
    def search(self, request):
        """
        Gợi ý từ đã có khi gõ. Query: q, lang, limit (≤ 50, mặc định 20). Từ bắt đầu bằng q trước (ngắn trước),
        chưa đủ thì thêm từ gần giống (pg_trgm). Chỉ từ đã tạo xong.
        """
        q = normalize_lookup(request.query_params.get('q') or '')[:100]
        language_code = request.query_params.get('lang')
        if not q or not language_code or len(language_code) > 10:
            return Response({'detail': 'q and lang are required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            limit = max(1, min(int(request.query_params.get('limit', 20)), 50))
        except ValueError:
            limit = 20

        words = AIWord.objects.filter(language_code=language_code, status='COMPLETED').exclude(is_deleted=True).exclude(is_active=False)
        found = list(words.filter(value__startswith=q).annotate(n=Length('value')).order_by('n', 'value')[:limit])
        if len(found) < limit and len(q) >= 2:
            seen = [w.id for w in found]
            found += list(
                words.exclude(id__in=seen).annotate(sim=TrigramSimilarity('value', q)).filter(sim__gt=0.3)
                .order_by('-sim', 'value')[:limit - len(found)]
            )
        return Response([{'id': w.id, 'value': w.value, 'language_code': w.language_code} for w in found])

    @action(detail=False, methods=['get'], url_path='suggest')
    def suggest(self, request):
        """
        Gợi ý khi thêm từ vào bộ. Query: q, lang (ngôn ngữ đang học), user_lang, mode = word | meaning, limit (≤ 20, mặc định 10).
        word: từ bắt đầu bằng q (rồi từ gần giống). meaning: q là nghĩa viết bằng user_lang ("nồi cơm"), trả từ của lang có bản dịch / định nghĩa khớp
        (thêm ngược khi chưa biết từ). Mỗi dòng {id (word_id), value, language_code, sense_id, pos, gloss, image_preview, matched}. Chỉ từ đã tạo xong.
        """
        from ai import suggest as suggestions

        q = (request.query_params.get('q') or '').strip()
        language_code = request.query_params.get('lang')
        user_language_code = request.query_params.get('user_lang') or ''
        mode = request.query_params.get('mode') or 'word'
        if not q or not is_lang(language_code) or mode not in ('word', 'meaning') or (user_language_code and not is_lang(user_language_code)):
            return Response({'detail': 'q, lang and mode (word | meaning) are required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            limit = max(1, min(int(request.query_params.get('limit', 10)), 20))
        except ValueError:
            limit = 10
        rows = (suggestions.by_meaning if mode == 'meaning' else suggestions.by_word)(q, language_code, user_language_code, limit)
        return Response(rows)

    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def regenerate(self, request, pk=None):
        """
        Admin: tạo lại 1 từ bằng AI (nghĩa sai / thiếu, hoặc mở lại từ bị REJECTED nhầm). Body: user_lang (tuỳ chọn).
        Nghĩa cũ vẫn dùng được tới khi bản mới lưu xong, sau đó thôi là nghĩa chính thức (bộ từ đang trỏ tới vẫn giữ);
        AI từ chối / lỗi thì giữ nguyên bản cũ.
        """
        word = AIWord.objects.filter(pk=pk).exclude(is_deleted=True).prefetch_related(Prefetch('senses', queryset=official_senses())).first()
        if word is None:
            return Response({'detail': 'Word not found'}, status=status.HTTP_404_NOT_FOUND)
        updated_at = word.updated_at or word.created_at
        if word.status == 'PROCESSING' and updated_at and (timezone.now() - updated_at).total_seconds() < settings.WORD_PROCESSING_STALE_SECONDS:
            return Response({'detail': 'Word is being generated'}, status=status.HTTP_409_CONFLICT)
        if word.status == 'BANNED':
            return Response({'detail': 'Word is banned, unban it first', 'errorCode': 'word_banned'}, status=status.HTTP_409_CONFLICT)
        user_language_code = request.data.get('user_lang') or settings.BASE_LANGUAGE[0]

        cache_manager = WordCacheManager()
        cache_manager.cache_word_clear_specific(word.language_code, word.value)
        old_ids = [str(s.id) for s in word.senses.all()]
        try:
            state, _ = start_word_generation(
                cache_manager, word.value, word.language_code, user_language_code, lambda: request.user, word,
                extra={'replace_sense_ids': old_ids} if old_ids else None,
            )
        except GenerationError:
            return Response({'detail': 'Service unavailable, try again later'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'detail': state, 'word_id': word.id, 'replacing': len(old_ids)}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=['delete'], url_path='clear', permission_classes=[IsAdminUser])
    def clear(self, request):
        """Xoá sạch từ, nghĩa, ảnh, bộ từ: chỉ dùng khi phát triển (DEBUG). Dữ liệu từ lưu sẵn ở ai_words_snapshot."""
        if not settings.DEBUG:
            return Response({'detail': 'Only available when DEBUG is on'}, status=status.HTTP_403_FORBIDDEN)
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
