"""
Lớp 1 kiểm chất lượng nghĩa bằng code (không gọi AI, không tốn tiền): bắt các lỗi máy móc đã gặp khi kiểm tay 20 từ ngẫu nhiên
(ví dụ dịch còn nguyên tiếng Anh, mục dịch sai chữ viết, loại từ lạ, số trong ví dụ bị mất...).

check_sense(sense, word, language_code, expected_langs) -> [issue]; check_word(senses, ...) thêm kiểm giữa các nghĩa (trùng nghĩa).
issue = {'code', 'severity' ('error' | 'warn'), 'field', 'lang', 'detail'}. `error` là lỗi chắc chắn (nên gọi lại đúng trường đó), `warn` là nghi ngờ.
`sense` là dict {pos, level, contents: {definition, usage, examples, translations}} như senses.json của seed_flow hoặc AISense.contents ở DB.
"""
import re

CEFR = {'A1', 'A2', 'B1', 'B2', 'C1', 'C2'}
CJK_LANGS = {'zh', 'ja', 'ko'}
# Từ loại chấp nhận (viết đầy đủ, chữ thường); dạng viết tắt / lạ có gợi ý đổi
POS_OK = {'noun', 'verb', 'adjective', 'adverb', 'pronoun', 'preposition', 'conjunction', 'determiner', 'interjection', 'article', 'particle',
          'numeral', 'auxiliary verb', 'modal verb', 'relative pronoun', 'phrase', 'phrasal verb', 'idiom', 'proverb', 'prefix', 'suffix',
          'classifier', 'postposition', 'abbreviation'}
POS_FIX = {'det': 'determiner', 'adj': 'adjective', 'adv': 'adverb', 'prep': 'preposition', 'conj': 'conjunction', 'pron': 'pronoun',
           'number': 'numeral', 'num': 'numeral', 'auxiliary': 'auxiliary verb', 'modal': 'modal verb', 'interj': 'interjection', 'v': 'verb', 'n': 'noun'}
ARCHAIC = re.compile(r'\b(archaic|obsolete|dated|dialect|dialectal|rare|old-fashioned|poetic)\b', re.I)
CJK = re.compile(r'[぀-ヿ㐀-䶿一-鿿가-힯]')
LATIN = re.compile(r'[A-Za-z]')
ZERO_WIDTH = re.compile('[​‌‍⁠﻿]')
HAS_LETTER = re.compile(r'[^\W\d_]', re.UNICODE)
TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)
MAX_DEF_CHARS = 300
MAX_TR_CHARS = 40
MAX_TR_ITEMS = 6


def _val(node, lang=None):
    """Chuỗi trong {'value': ...} / chuỗi trần / dict theo ngôn ngữ."""
    if lang is not None and isinstance(node, dict):
        node = node.get(lang)
    if isinstance(node, dict):
        node = node.get('value')
    return node.strip() if isinstance(node, str) else ''


def _issue(code, severity, field, lang='', detail=''):
    return {'code': code, 'severity': severity, 'field': field, 'lang': lang, 'detail': detail}


def _words(text):
    return [t.lower() for t in TOKEN.findall(text or '')]


def check_sense(sense, word, language_code='en', expected_langs=()):
    out = []
    contents = sense.get('contents') or {}
    definition = contents.get('definition') or {}
    examples = [e for e in (contents.get('examples') or []) if isinstance(e, dict)]
    translations = contents.get('translations') or {}
    source = language_code or 'en'
    targets = [l for l in expected_langs if l != source]

    # --- loại từ, trình độ ---
    pos = (sense.get('pos') or '').strip()
    if not pos:
        out.append(_issue('pos_missing', 'error', 'pos'))
    elif pos not in POS_OK:
        fix = POS_FIX.get(pos.lower())
        out.append(_issue('pos_nonstandard', 'error' if fix else 'warn', 'pos', detail=f'{pos!r}' + (f' -> {fix!r}' if fix else '')))
    level = (sense.get('level') or '').strip()
    if source == 'en' and level and level not in CEFR:
        out.append(_issue('level_invalid', 'error', 'level', detail=level))

    # --- định nghĩa gốc ---
    base_def = _val(definition, source)
    if not base_def:
        out.append(_issue('def_missing', 'error', 'definition', source))
    else:
        if len(base_def) > MAX_DEF_CHARS:
            out.append(_issue('def_too_long', 'warn', 'definition', source, str(len(base_def))))
        if ARCHAIC.search(base_def):
            out.append(_issue('def_archaic', 'warn', 'definition', source, ARCHAIC.search(base_def).group(0)))
        if word and len(word) >= 4 and re.search(rf'\b{re.escape(word)}\b', base_def, re.I) and not re.search(r'\b(past|participle|plural|form|tense)\b', base_def, re.I):
            out.append(_issue('def_has_headword', 'warn', 'definition', source, word))

    # --- ví dụ gốc phải có từ (hoặc dạng biến) ---
    stem = (word or '').lower()[:len(word) - 2] if source == 'en' and word and ' ' not in word and len(word) >= 6 else ''  # từ ngắn hay bất quy tắc (has / had / was): bỏ qua
    for i, ex in enumerate(examples):
        text = _val(ex, source)
        if not text:
            out.append(_issue('example_missing', 'error', f'examples[{i}]', source))
        elif stem and stem not in text.lower():
            out.append(_issue('example_lacks_word', 'warn', f'examples[{i}]', source, text[:60]))

    # --- bản dịch định nghĩa / ví dụ / danh sách dịch ---
    for lang in targets:
        d = _val(definition, lang)
        if not d:
            out.append(_issue('def_translation_missing', 'error', 'definition', lang))
        else:
            if base_def and d == base_def:
                out.append(_issue('def_untranslated', 'error', 'definition', lang))
            if ZERO_WIDTH.search(d):
                out.append(_issue('zero_width', 'error', 'definition', lang))
            _script_check(out, d, lang, 'definition', lang)
            toks = _words(d)
            for k, t in enumerate(toks):
                if len(t) >= 5 and t in toks[k + 1:k + 4] and lang not in CJK_LANGS | {'vi'}:  # vi: âm tiết lặp là bình thường
                    out.append(_issue('repeated_word', 'warn', 'definition', lang, t))
                    break
        for i, ex in enumerate(examples):
            en = _val(ex, source)
            tr = _val(ex, lang)
            field = f'examples[{i}]'
            if not tr:
                out.append(_issue('example_translation_missing', 'error', field, lang))
                continue
            if en and tr == en and LATIN.search(en) and lang != 'en':
                out.append(_issue('example_untranslated', 'error', field, lang, en[:50]))
                continue
            _script_check(out, tr, lang, field, lang)
            digits_en, digits_tr = set(re.findall(r'\d+', en)), set(re.findall(r'\d+', tr))
            if digits_en and not digits_en <= digits_tr:
                out.append(_issue('example_number_lost', 'error', field, lang, f'{sorted(digits_en)} vs {sorted(digits_tr)}'))
            if en and tr:
                ratio = len(tr) / max(len(en), 1)
                lo, hi = (0.12, 1.1) if lang in CJK_LANGS else (0.45, 2.3)
                if source in CJK_LANGS:
                    lo, hi = (0.9, 8.0) if lang not in CJK_LANGS else (0.4, 2.5)
                if not lo <= ratio <= hi:
                    out.append(_issue('example_length_ratio', 'warn', field, lang, f'{ratio:.2f}'))
        items = translations.get(lang)
        if not isinstance(items, list) or not [x for x in items if (_val(x) if isinstance(x, (dict, str)) else '')]:
            out.append(_issue('translations_empty', 'error', 'translations', lang))
            continue
        texts = [_val(x) for x in items if _val(x)]
        if len(texts) != len({t.lower() for t in texts}):
            out.append(_issue('translations_duplicate', 'warn', 'translations', lang))
        if len(texts) > MAX_TR_ITEMS:
            out.append(_issue('translations_many', 'warn', 'translations', lang, str(len(texts))))
        for t in texts:
            if len(t) > MAX_TR_CHARS:
                out.append(_issue('translation_long', 'warn', 'translations', lang, t[:50]))
            if ZERO_WIDTH.search(t):
                out.append(_issue('zero_width', 'error', 'translations', lang, t))
            _script_check(out, t, lang, 'translations', lang)
            if word and t.lower() == word.lower() and lang in CJK_LANGS | {'vi'}:
                out.append(_issue('translation_is_headword', 'error' if lang != 'vi' else 'warn', 'translations', lang, t))  # vi: vay mượn (yeah) hợp lệ
            if lang in CJK_LANGS and len(t) == 1 and not CJK.search(t) and HAS_LETTER.search(t):
                out.append(_issue('translation_fragment', 'warn', 'translations', lang, t))
    return out


def _script_check(out, text, lang, field, tag):
    """Chữ viết sai ngôn ngữ: CJK đích có chữ Latin (trừ tên riêng viết tắt ngắn), ngôn ngữ Latin chứa CJK."""
    if not HAS_LETTER.search(text):  # chỉ chữ số / dấu câu: số 1, 2... hợp lệ ở mọi ngôn ngữ
        return
    if lang in CJK_LANGS:
        if not CJK.search(text):
            out.append(_issue('wrong_script', 'error', field, tag, text[:40]))
    elif CJK.search(text):
        out.append(_issue('wrong_script', 'error', field, tag, text[:40]))


def check_word(senses, word, language_code='en', expected_langs=()):
    """Kiểm từng nghĩa + trùng nghĩa giữa các nghĩa cùng từ. Trả [(chỉ số nghĩa hoặc None, issue)]."""
    out = []
    for i, sense in enumerate(senses):
        out.extend((i, iss) for iss in check_sense(sense, word, language_code, expected_langs))
    source = language_code or 'en'
    defs = [(i, (s.get('pos') or '').lower(), set(_words(_val((s.get('contents') or {}).get('definition'), source)))) for i, s in enumerate(senses)]
    for a in range(len(defs)):
        for b in range(a + 1, len(defs)):
            (i, pa, wa), (j, pb, wb) = defs[a], defs[b]
            if pa == pb and wa and wb and len(wa & wb) / len(wa | wb) >= 0.8:
                out.append((j, _issue('duplicate_sense', 'warn', 'definition', source, f'giống nghĩa #{i}')))
    return out


def summarize(issues):
    """[(idx, issue)] hoặc [issue] -> {'error': n, 'warn': n, 'by_code': {...}}"""
    flat = [x[1] if isinstance(x, tuple) else x for x in issues]
    by_code = {}
    for iss in flat:
        by_code[iss['code']] = by_code.get(iss['code'], 0) + 1
    return {'error': sum(1 for i in flat if i['severity'] == 'error'), 'warn': sum(1 for i in flat if i['severity'] == 'warn'), 'by_code': by_code}
