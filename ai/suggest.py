"""
Gợi ý từ khi người dùng gõ (thêm từ vào bộ):
- mode "word": gõ từ của ngôn ngữ đang học, trả từ bắt đầu bằng chuỗi đó (rồi từ gần giống) kèm nghĩa ngắn để nhận ra;
- mode "meaning": gõ nghĩa bằng ngôn ngữ của mình (vd tiếng Việt "nồi cơm"), trả từ của ngôn ngữ đang học có bản dịch / định nghĩa khớp
  (thêm ngược: người học chưa biết từ tiếng Anh vẫn tìm được từ).
Chỉ nghĩa chính thức của từ đã tạo xong. Tìm theo nghĩa dùng bản dịch ngắn (contents.translations[user_lang]) trước, định nghĩa sau.
"""
import re
import unicodedata

from django.contrib.postgres.lookups import Unaccent
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import TextField
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Length

from ai.models.AISense import AISense
from ai.models.AIWord import AIWord
from utils.utils.normalize import normalize_lookup

MAX_Q = 60


def fold(text):
    """Bỏ dấu và đưa về chữ thường (nồi -> noi, Đại -> dai) để tìm không phân biệt dấu."""
    text = unicodedata.normalize('NFD', (text or '').lower().replace('đ', 'd'))
    return ''.join(ch for ch in text if unicodedata.category(ch) != 'Mn')


def _sense_row(sense, word_value, user_language_code, matched=None):
    contents = sense.contents if isinstance(sense.contents, dict) else {}
    translations = (contents.get('translations') or {}).get(user_language_code) or []
    definition = ((contents.get('definition') or {}).get(user_language_code) or (contents.get('definition') or {}).get(sense.language_code) or {})
    gloss = ', '.join(translations[:3]) or (definition.get('value') if isinstance(definition, dict) else '') or ''
    return {
        'id': str(sense.word_id), 'value': word_value, 'language_code': sense.language_code, 'sense_id': str(sense.id),
        'pos': sense.pos, 'gloss': gloss, 'image_preview': sense.image_preview, 'matched': matched, 'offensive': bool(sense.is_offensive),
    }


def _official_senses(language_code, offensive=False):
    """Nghĩa chính thức dùng để gợi ý. `offensive`: người dùng đã bật hiện nội dung thô tục (mặc định ẩn nghĩa is_offensive)."""
    senses = AISense.objects.filter(
        language_code=language_code, is_official=True, word__status='COMPLETED',
    ).exclude(is_deleted=True).exclude(is_active=False).exclude(word__is_deleted=True).exclude(word__is_active=False)
    return senses if offensive else senses.exclude(is_offensive=True)


def has_offensive_sense(word_id, language_code):
    return AISense.objects.filter(word_id=word_id, language_code=language_code, is_official=True, is_offensive=True).exclude(is_deleted=True).exists()


def by_word(q, language_code, user_language_code, limit, offensive=False):
    q = normalize_lookup(q)[:MAX_Q]
    if not q:
        return []
    words = AIWord.objects.filter(language_code=language_code, status='COMPLETED').exclude(is_deleted=True).exclude(is_active=False)
    found = list(words.filter(value__startswith=q).annotate(n=Length('value')).order_by('n', 'value')[:limit])
    if len(found) < limit and len(q) >= 2:
        seen = [w.id for w in found]
        found += list(words.exclude(id__in=seen).annotate(sim=TrigramSimilarity('value', q)).filter(sim__gt=0.3).order_by('-sim', 'value')[:limit - len(found)])
    rows = [_first_sense_row(w, user_language_code, offensive) for w in found]
    return [r for r in rows if r is not None]


def _first_sense_row(word, user_language_code, offensive=False):
    sense = _official_senses(word.language_code, offensive).filter(word_id=word.id).order_by('-score', '-like_count', 'id').first()
    if sense is None:
        if not offensive and has_offensive_sense(word.id, word.language_code):
            return None  # từ chỉ có nghĩa thô tục: không gợi ý cho người chưa bật
        return {'id': str(word.id), 'value': word.value, 'language_code': word.language_code, 'sense_id': None, 'pos': None, 'gloss': '', 'image_preview': None, 'matched': None}
    return _sense_row(sense, word.value, user_language_code)


def by_meaning(q, language_code, user_language_code, limit, offensive=False):
    """Từ của `language_code` có nghĩa khớp chuỗi `q` viết bằng `user_language_code`."""
    q_raw = ' '.join((q or '').lower().split())[:MAX_Q]
    q = fold(q_raw)
    if len(q) < 2 or not user_language_code:
        return []
    # Cột và chuỗi gõ cùng bỏ dấu (extension unaccent): gõ "nha" vẫn ra "nhà"
    translations_text = Unaccent(Cast(KeyTextTransform(user_language_code, KeyTextTransform('translations', 'contents')), TextField()))
    definition_text = Unaccent(Cast(KeyTextTransform('value', KeyTextTransform(user_language_code, KeyTextTransform('definition', 'contents'))), TextField()))
    # Khớp từ bắt đầu bằng q ở đầu một từ (không khớp giữa từ: "nhà" không khớp "nhẹ nhàng" ở giữa, nhưng gõ dở "nh" vẫn thấy "nhà")
    boundary = r'(^|[^[:alnum:]])' + re.escape(q)
    candidates = _official_senses(language_code, offensive).select_related('word').annotate(tr_text=translations_text)         .filter(tr_text__iregex=boundary).order_by('id')[:300]
    best = {}
    for sense in candidates:
        contents = sense.contents if isinstance(sense.contents, dict) else {}
        translations = [str(t) for t in ((contents.get('translations') or {}).get(user_language_code) or [])]
        score, matched = 0, None
        for t in translations:
            low = fold(t).strip()
            s = 4 if low == q else 3 if low.startswith(q) else 2 if re.search(boundary, low) else 0
            if s and q_raw != q and t.lower().strip().startswith(q_raw):
                s += 0.5  # người gõ có dấu: khớp đúng dấu xếp trước khớp bỏ dấu ("của" trước "cua")
            if s > score:
                score, matched = s, t
        if not score:
            continue
        score = score * 1000 + min(sense.like_count or 0, 99) - min(len(sense.word_value), 40) / 100
        current = best.get(sense.word_id)
        if current is None or score > current[0]:
            best[sense.word_id] = (score, sense, matched)
    ranked = sorted(best.values(), key=lambda x: -x[0])[:limit]
    rows = [_sense_row(sense, sense.word_value, user_language_code, matched) for _, sense, matched in ranked]
    if len(rows) < min(limit, 3):
        # Bản dịch ngắn khớp quá ít: thêm các từ có định nghĩa khớp (xếp sau, ít chính xác hơn nên chỉ lấp chỗ trống)
        seen = {r['id'] for r in rows}
        extra = _official_senses(language_code, offensive).annotate(def_text=definition_text).filter(def_text__iregex=boundary).exclude(word_id__in=seen).order_by('id')[:limit * 3]
        for sense in extra:
            if str(sense.word_id) in seen:
                continue
            seen.add(str(sense.word_id))
            rows.append(_sense_row(sense, sense.word_value, user_language_code))
            if len(rows) >= limit:
                break
    return rows
