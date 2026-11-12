"""
Tạo / kiểm tra từ hàng loạt. Là phần của từ điển (ai-words/bulk-create), bộ từ gọi lại các hàm này
(collections/bulk-create, user-collections/{id}/add-words) và truyền thêm collection_id để từ tạo xong được thêm vào bộ.
"""
from ai.forms import guess_lemmas, resolve_form
from ai.generation import GenerationError, start_word_generation
from ai.models import AISense, AIWord
from ai.validators import is_uuid
from utils import morph
from utils.redis.client import get_queue_client
from utils.redis.word_init import WordCacheManager
from utils.utils.normalize import is_lookup_word, lookup_entry_problem, normalize_lookup

BULK_WORD_QUEUE = 'redis_collection_word'  # worker bỏ qua phần thêm vào bộ khi không có bộ nào gắn vào từ


def ranked_official_senses(words, language_code):
    """
    Nghĩa chính thức của các từ, theo từ rồi theo độ tốt giảm dần: score, rồi like, rồi id (UUIDv7 tăng theo thời điểm sinh
    nên bằng nhau thì lấy nghĩa AI trả ra trước = phổ biến nhất). score chưa có cơ chế tự cập nhật (hiện đều 0) nên tạm thời
    like quyết định; sau này score tổng hợp like, lượt xem, uy tín người tạo thì thứ tự tự theo. Không lấy nghĩa tuỳ chỉnh của ai.
    """
    return (
        AISense.objects.filter(is_official=True, language_code=language_code, word_value__in=list(words))
        .exclude(is_deleted=True).exclude(is_active=False)
        .order_by('word_value', '-score', '-like_count', 'id')
    )


def best_official_senses(words, language_code):
    """Chế độ nhanh khi thêm từ vào bộ: mỗi từ 1 nghĩa chính thức tốt nhất (score cao nhất)."""
    return ranked_official_senses(words, language_code).distinct('word_value')


def sense_brief(sense, user_language_code):
    """Dòng gọn của 1 nghĩa để người dùng chọn: loại từ, cấp, định nghĩa / từ tương đương theo ngôn ngữ của họ, ví dụ đầu tiên."""
    contents = sense.contents or {}
    lang = sense.language_code

    def text(node):
        return (node or {}).get('value') or ''

    definition = contents.get('definition') or {}
    translations = contents.get('translations') or {}
    examples = contents.get('examples') or [{}]
    return {
        'id': str(sense.id), 'pos': sense.pos, 'level': sense.level, 'like_count': sense.like_count,
        'definition': text(definition.get(user_language_code)) or text(definition.get(lang)),
        'translations': translations.get(user_language_code) or [],
        'example': text((examples[0] or {}).get(lang)),
        'image_preview': sense.image_preview, 'offensive': bool(sense.is_offensive),
    }


def clean_words(words):
    """Danh sách từ client gửi -> từ đã chuẩn hoá (giống get-word / worker, không bỏ ký tự), bỏ trùng, giữ thứ tự."""
    return list(dict.fromkeys(w for w in (normalize_lookup(w) for w in words if isinstance(w, str)) if is_lookup_word(w)))


def fold_forms(words, language_code):
    """
    Dạng biến cách (killed, ran, books) chưa có mục riêng thì đổi thành từ gốc (kill, run, book): từ gốc đã có thì dùng nghĩa của nó, chưa có thì
    tạo từ gốc, không tạo mục riêng cho dạng chia (cùng luật với get-word, ai/forms.py). Chuỗi đã có mục riêng (kể cả đang tạo / bị từ chối) giữ nguyên.
    Chỉ ngôn ngữ có bộ sinh dạng chia đủ tin cậy (en, ru, uk). `words` đã qua clean_words.
    Trả (từ sau khi đổi, bỏ trùng, giữ thứ tự; {dạng: từ gốc}).
    """
    if not words or not morph.supports_paradigm(language_code):
        return list(words), {}
    have = set(
        AISense.objects.filter(language_code=language_code, word_value__in=words, is_official=True).exclude(is_deleted=True)
        .values_list('word_value', flat=True)
    ) | set(AIWord.objects.filter(language_code=language_code, value__in=words).exclude(is_deleted=True).values_list('value', flat=True))
    form_of = {}
    for word in words:
        if word in have:
            continue
        candidates = resolve_form(word, language_code)
        lemma = candidates[0].word.value if candidates else None
        if lemma is None:
            guesses = guess_lemmas(word, language_code)
            lemma = guesses[0].lemma if guesses else None
        if lemma and lemma != word:
            form_of[word] = lemma
    return list(dict.fromkeys(form_of.get(w, w) for w in words)), form_of


def apply_form_of(results, form_of):
    """Kết quả theo từ gốc (describe_words) -> thêm dòng cho dạng đã gõ: cùng trạng thái với từ gốc kèm `form_of`."""
    for form, lemma in form_of.items():
        results[form] = {**{k: v for k, v in results.get(lemma, {'status': 'unknown'}).items() if k != 'senses'}, 'form_of': lemma}
    return results


def sort_words(words, language_code):
    """
    Chia từ thành: found {từ: nghĩa chính thức tốt nhất}, invalid (bị từ chối, hoặc là câu / quá dài nên không phải mục từ),
    pending (chờ AI), to_start (cần đẩy AI tạo: chưa có bản ghi hoặc lần trước lỗi), existing {từ: AIWord}.
    """
    found = {sense.word_value: sense for sense in best_official_senses(words, language_code)}
    missing = [w for w in words if w not in found]
    existing = {w.value: w for w in AIWord.objects.filter(value__in=missing, language_code=language_code).exclude(is_deleted=True)} if missing else {}
    invalid = [w for w in missing if w in existing and existing[w].status in ('REJECTED', 'INVALID', 'BANNED')]
    # Câu bất kỳ không phải mục từ: loại ngay, không tốn lượt AI (từ đã có trong DB vẫn được giữ ở trên)
    invalid += [w for w in missing if w not in existing and lookup_entry_problem(w, language_code)]
    pending = [w for w in missing if w not in invalid]
    to_start = [w for w in pending if w not in existing or existing[w].status == 'FAILED']
    return found, invalid, pending, to_start, existing


def apply_sense_choices(found, choices, language_code):
    """
    Người dùng đã chọn đúng nghĩa cho một số từ (gợi ý theo nghĩa tiếng mẹ đẻ): `choices` {từ: sense_id}. Thay nghĩa "tốt nhất" bằng
    nghĩa đã chọn khi nó là nghĩa chính thức của đúng từ đó; chọn sai / không hợp lệ thì giữ nghĩa mặc định. Sửa `found` tại chỗ.
    """
    if not isinstance(choices, dict) or not choices:
        return found
    wanted = {normalize_lookup(str(w)): str(s) for w, s in list(choices.items())[:500] if is_uuid(str(s))}
    wanted = {w: s for w, s in wanted.items() if w in found}
    if not wanted:
        return found
    senses = {str(s.id): s for s in AISense.objects.filter(id__in=wanted.values(), language_code=language_code, is_official=True).exclude(is_deleted=True).exclude(is_active=False)}
    for word, sense_id in wanted.items():
        sense = senses.get(sense_id)
        if sense is not None and sense.word_value == word:
            found[word] = sense
    return found


def start_words(user, to_start, existing, language_code, user_language_code, collection_id=None):
    """
    Đẩy AI tạo các từ chưa có (gọi sau khi DB đã commit). Trả tập từ đã nhận việc hoặc đang có người tạo.
    collection_id: bộ cần thêm từ khi tạo xong (worker / retrieve thêm qua pending_words); không có thì chỉ tạo từ.
    """
    cache_manager = WordCacheManager()
    queue = get_queue_client() if collection_id else None
    started = set()
    for word in to_start:
        try:
            state, _ = start_word_generation(
                cache_manager, word, language_code, user_language_code, lambda: user,
                existing=existing.get(word), queue=BULK_WORD_QUEUE,
            )
        except GenerationError:
            continue  # từ này ở lại pending, lần sau tạo lại
        started.add(word)
        if state == 'started' and collection_id:
            queue.sadd(f'collection_id:{word}', str(collection_id))
    return started


def describe_words(requested, language_code, found, invalid, pending, to_start, existing, started=()):
    """
    Kết quả cho app, khoá là từ đã chuẩn hoá: exists (đã có nghĩa chính thức), processing (đang tạo),
    rejected (từ bị từ chối / chặn), invalid + reason (không phải mục từ: sentence | too_long | not_a_word),
    unknown (chưa có và chưa nhận việc tạo, hoặc việc tạo lỗi).
    """
    cleaned = set(found) | set(invalid) | set(pending)
    results = {}
    for raw in requested:  # không tra được (rỗng, chỉ dấu câu, quá 100 ký tự): báo thẳng để app không treo chờ
        key = normalize_lookup(raw) if isinstance(raw, str) else ''
        if key and key not in cleaned:
            results[key] = {'status': 'invalid', 'reason': 'not_a_word'}
    for key in found:
        results[key] = {'status': 'exists'}
    for key in invalid:
        problem = lookup_entry_problem(key, language_code) if key not in existing else None
        results[key] = {'status': 'invalid', 'reason': problem} if problem else {'status': 'rejected'}
    for key in pending:
        results[key] = {'status': 'unknown'} if key in to_start and key not in started else {'status': 'processing'}
    return results
