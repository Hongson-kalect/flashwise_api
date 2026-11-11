"""
Lớp 2 kiểm chất lượng bản dịch: một model KHÁC họ (Claude Haiku, vì bản dịch do Gemini sinh) đọc các nghĩa của một từ cùng bản dịch và, với MỖI
(nghĩa, ngôn ngữ), phải nói rõ "ổn" hoặc đưa bản sửa. Im lặng không được tính là đồng ý.

Kết quả ghi vào AISense (`verify_state` theo từng ngôn ngữ, `ai_verify` tóm tắt, `verify` là của người kiểm, không bao giờ bị AI đổi):
  ok       Haiku đồng ý với bản của Gemini -> ai_verify = True (2 nhà cùng đồng ý)
  fixed    Haiku trả bản mới: áp bản mới, ai_verify = False, bản cũ ghi trong `changes` (chờ người kiểm)
  flagged  Haiku nói sai nhưng không có bản sửa dùng được, hoặc bản sửa vi phạm kiểm tra lớp 1: giữ nguyên, ai_verify = False
Nghĩa có verify = True (người đã kiểm) bị bỏ qua hoàn toàn.

build_request(...) -> yêu cầu cho ai.seedflow.llm (batch hoặc online); evaluate(...) -> kết quả theo (nghĩa, ngôn ngữ);
apply_to_senses(...) ghi vào DB.
"""
import copy
import re

from django.conf import settings
from django.utils import timezone

from ai.seedflow import quality
from utils.ai import claude

# Lý do cho thấy model chỉ "làm đẹp" / thêm cho đủ chứ không phải sửa lỗi (đo trên 20 từ: nhiều bản sửa kiểu này)
NOT_A_FIX = re.compile(r'\b(no change|fine as is|is fine|acceptable|no [a-z ,/-]{0,40}issue|not an issue|reorder|enrichment|too thin|minor|stylistic|more natural|slightly)\b', re.I)
FIELDS = ['definition', 'example_1', 'example_2', 'translations', 'sense_issue']
SYSTEM = (
    "You are a strict bilingual lexicographer auditing machine-generated entries of a learner's dictionary. "
    "The English text is correct and is the source of truth. You audit ONLY the translations into the other languages. "
    "Your job is to FIND ERRORS: machine translation is wrong far more often than it looks, so read every item critically "
    "and prefer reporting a doubtful item over letting it pass."
)
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "checks": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {
                "sense": {"type": "INTEGER"},
                "lang": {"type": "STRING"},
                "back": {"type": "STRING"},
                "verdict": {"type": "STRING", "enum": ["ok", "fix"]},
                "score": {"type": "INTEGER"},
            },
            "required": ["sense", "lang", "back", "verdict", "score"],
        }},
        "fixes": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {
                "sense": {"type": "INTEGER"},
                "field": {"type": "STRING", "enum": FIELDS},
                "lang": {"type": "STRING"},
                "value": {"type": "STRING"},
                "reason": {"type": "STRING"},
                "score": {"type": "INTEGER"},
            },
            "required": ["sense", "field", "lang", "value", "reason", "score"],
        }},
    },
    "required": ["checks", "fixes"],
}
RULES = """Audit every (sense, target language) pair. For each pair, read the definition, the example translations and the translation list against the English entry of THAT sense.

Method, in this order:
a. Back-translate: for each item of the translation list and for the definition, think what it means in English. Compare with the English definition.
b. Walk this checklist and report anything that fails:
 1. Definition: same meaning as the English one; not a different sense, nothing added or dropped (a much shorter or longer text usually dropped or added something); correct grammar; no repeated word or near-duplicate phrase; for a verb sense the English "To ..." must be rendered as a plain dictionary definition, WITHOUT an infinitive particle ("Para ...", "Pour ...", "Để ...", "...する", etc.).
 2. Example: faithful translation of the English example: same meaning, tense, numbers, times, names, polarity, and the SAME word sense (an example of an unusual sense must not be translated with the word of the common sense, e.g. "metal" = crushed road stone is not "métal"/"kim loại"); natural wording.
 3. Translation list: short words a learner would look up for exactly this definition. Report an item that is: the translation of a word from the example instead of the headword; a word of another sense of the English word; a different part of speech than the entry (a noun for a verb sense, a verb for an adjective sense, etc.); a fragment of a longer expression; carrying an article, plural marker such as "(s)", or a leading particle; a duplicate or near-duplicate of another item; archaic, rare, regional, or merely related but not synonymous; wrong gender or form for the language; or contradicting the definition. Also report when an item a native speaker would obviously use is missing and the list is thin or empty-ish. At most 5 items, most common first.
 4. English side: if the English sense is archaic, obsolete or very rare, or its example shows a different sense or is ungrammatical, report it with field sense_issue (lang "en").
Vietnamese must be natural Vietnamese; Chinese simplified; Spanish neutral; French standard; Japanese and Korean standard polite-neutral.

Lines starting with AUTOMATIC CHECK FAILED report a certain error found by a script: you MUST fix it and give that pair the verdict "fix".

Answer with two lists.
- checks: exactly one row for EVERY (sense, language) pair listed in the entries. back: your back-translation notes, at most 25 words (scratch space, write it BEFORE you decide). verdict: "ok" only if you checked every item and found nothing wrong, otherwise "fix". score: 0-100 integer. For "ok" it is how sure you are that the whole pair is correct (100 = certain; use 60 or less when you are unsure about any word, a rare sense, or a language you handle less well). For "fix" it is how sure you are that your proposed correction is itself right.
- fixes: one row per corrected field of a pair with verdict "fix": field (definition, example_1, example_2, translations, sense_issue), lang, the full corrected text as value (for translations the full corrected list, items separated by " | "), a reason of at most 12 words, and score (0-100) = how sure you are that the corrected text is right. The value MUST differ from the current text. Do not report pure style preferences. For sense_issue use lang "en" and describe the problem in value."""


def _text(node, lang):
    value = (node or {}).get(lang) if isinstance(node, dict) else None
    if isinstance(value, dict):
        value = value.get('value')
    return value.strip() if isinstance(value, str) else ''


def sense_langs(sense, langs, language_code='en'):
    """Ngôn ngữ đích mà nghĩa này đã có nội dung (định nghĩa hoặc danh sách dịch), trong số `langs`."""
    c = sense.get('contents') or {}
    have = {l for l in (c.get('definition') or {}) if _text(c.get('definition'), l)} | {l for l, v in (c.get('translations') or {}).items() if v}
    return [l for l in langs if l != language_code and l in have]


def render_prompt(word, language_code, senses, langs, hints=None):
    hints = hints or {}
    targets = [l for l in langs if l != language_code]
    parts = [f"Headword: '{word}' ({language_code}). Target languages: {', '.join(targets)}.", '', RULES, '', '<entries>']
    for i, s in enumerate(senses):
        c = s.get('contents') or {}
        examples = [e for e in (c.get('examples') or []) if isinstance(e, dict)][:2]
        parts.append(f"<sense i=\"{i}\" pos=\"{s.get('pos') or ''}\">")
        parts.append(f"en definition: {_text(c.get('definition'), language_code)}")
        for k, e in enumerate(examples, 1):
            parts.append(f"en example_{k}: {_text(e, language_code)}")
        for lang in sense_langs(s, targets, language_code):
            if _text(c.get('definition'), lang):
                parts.append(f"[{lang}] definition: {_text(c.get('definition'), lang)}")
            for k, e in enumerate(examples, 1):
                if _text(e, lang):
                    parts.append(f"[{lang}] example_{k}: {_text(e, lang)}")
            items = (c.get('translations') or {}).get(lang)
            if items:
                parts.append(f"[{lang}] translations: " + ' | '.join(x if isinstance(x, str) else str((x or {}).get('value', '')) for x in items))
            for hint in hints.get((i, lang), []):
                parts.append(f"[{lang}] AUTOMATIC CHECK FAILED: {hint}")
        parts.append('</sense>')
    parts.append('</entries>')
    return '\n'.join(parts)


def build_request(custom_id, word, language_code, senses, langs, model=None, effort='low', hints=None):
    params = {
        'model': model or settings.AI_REFINE_MODELS[0], 'max_tokens': 8000, 'system': SYSTEM,
        'messages': [{'role': 'user', 'content': render_prompt(word, language_code, senses, langs, hints)}],
        'thinking': {'type': 'disabled'},
        'output_config': claude.output_config(copy.deepcopy(SCHEMA), effort=effort),
    }
    return {'custom_id': custom_id, 'params': params}


def parse_fixes(data, senses, langs, language_code='en'):
    """Giữ bản sửa hợp lệ: nghĩa tồn tại, trường nằm trong FIELDS, ngôn ngữ thuộc đích, có giá trị."""
    out = []
    for fix in (data or {}).get('fixes') or []:
        if not isinstance(fix, dict):
            continue
        i, field, lang, value = fix.get('sense'), fix.get('field'), fix.get('lang'), (fix.get('value') or '').strip()
        if not isinstance(i, int) or not 0 <= i < len(senses) or field not in FIELDS or not value:
            continue
        if field != 'sense_issue' and (lang not in langs or lang == language_code):
            continue
        out.append({'sense': i, 'field': field, 'lang': lang, 'value': value, 'reason': (fix.get('reason') or '')[:120],
                    'score': _score(fix.get('score'))})
    return out


def _score(value):
    """Điểm 0-100 model tự chấm; None nếu không có / không hợp lệ."""
    return max(0, min(100, value)) if isinstance(value, int) and not isinstance(value, bool) else None


def parse_checks(data, senses, langs, language_code='en'):
    """{(nghĩa, ngôn ngữ): {'verdict': ok|fix, 'score': int|None}} cho các cặp thật sự có trong prompt (dòng đầu thắng nếu trùng)."""
    out = {}
    for item in (data or {}).get('checks') or []:
        if not isinstance(item, dict) or not isinstance(item.get('sense'), int) or not 0 <= item['sense'] < len(senses):
            continue
        lang = item.get('lang')
        if lang not in sense_langs(senses[item['sense']], langs, language_code) or item.get('verdict') not in ('ok', 'fix'):
            continue
        out.setdefault((item['sense'], lang), {'verdict': item['verdict'], 'score': _score(item.get('score'))})
    return out


def parse_ok(data, senses, langs, language_code='en'):
    """{(nghĩa, ngôn ngữ)} model nói là ổn (chỉ những cặp thật sự có trong prompt). Nhận cả dạng cũ `ok` lẫn `checks`."""
    out = {pair for pair, c in parse_checks(data, senses, langs, language_code).items() if c['verdict'] == 'ok'}
    for item in (data or {}).get('ok') or []:
        if not isinstance(item, dict) or not isinstance(item.get('sense'), int) or not 0 <= item['sense'] < len(senses):
            continue
        valid = set(sense_langs(senses[item['sense']], langs, language_code))
        out |= {(item['sense'], l) for l in (item.get('langs') or []) if l in valid}
    return out


def current_value(senses, fix):
    """Văn bản hiện có của trường mà bản sửa nhắm tới ('' nếu chưa có); translations trả chuỗi 'a | b'."""
    c = senses[fix['sense']].get('contents') or {}
    if fix['field'] == 'definition':
        return _text(c.get('definition'), fix['lang'])
    if fix['field'].startswith('example_'):
        idx = int(fix['field'].split('_')[1]) - 1
        examples = c.get('examples') or []
        return _text(examples[idx], fix['lang']) if idx < len(examples) and isinstance(examples[idx], dict) else ''
    if fix['field'] == 'translations':
        return ' | '.join((x if isinstance(x, str) else str((x or {}).get('value', ''))).strip() for x in ((c.get('translations') or {}).get(fix['lang']) or []))
    return ''


def drop_noops(senses, fixes):
    """Bỏ bản sửa không đổi gì (model hay trả lại nguyên văn kèm lý do 'ổn') hoặc có lý do làm đẹp. Trả (bản sửa thật, số bản bị bỏ)."""
    real = [f for f in fixes if (f['field'] == 'sense_issue' and not NOT_A_FIX.search(f['value'] + ' ' + f['reason']))
            or (f['field'] != 'sense_issue' and current_value(senses, f).strip() != f['value'].strip() and not NOT_A_FIX.search(f['reason']))]
    return real, len(fixes) - len(real)


def apply_fixes(senses, fixes, language_code='en'):
    """Sửa tại chỗ. sense_issue không sửa gì. Trả (số bản sửa đã áp, [{field, lang, before, after, reason}])."""
    applied = []
    for fix in fixes:
        if fix['field'] == 'sense_issue':
            continue
        contents = senses[fix['sense']].setdefault('contents', {})
        lang, value = fix['lang'], fix['value']
        if fix['field'] == 'definition':
            node = contents.setdefault('definition', {})
            before = _text(node, lang)
            node[lang] = {'value': value} if isinstance(node.get(lang), dict) else value
        elif fix['field'].startswith('example_'):
            idx = int(fix['field'].split('_')[1]) - 1
            examples = contents.get('examples') or []
            if idx >= len(examples) or not isinstance(examples[idx], dict):
                continue
            before = _text(examples[idx], lang)
            examples[idx][lang] = {'value': value} if isinstance(examples[idx].get(lang), dict) else value
        else:
            items = [x.strip() for x in value.split('|') if x.strip()][:5]
            if not items:
                continue
            node = contents.setdefault('translations', {})
            before = ' | '.join(str(x) for x in (node.get(lang) or []))
            node[lang] = items
            value = ' | '.join(items)
        if before.strip() == value.strip():
            continue
        applied.append({'field': fix['field'], 'lang': lang, 'before': before, 'after': value, 'reason': fix.get('reason', '')})
    return len(applied), applied


def recheck(word, language_code, senses, langs):
    """Lớp 1 trên kết quả sau khi sửa; trả các lỗi mức error."""
    return [(i, iss) for i, iss in quality.check_word(senses, word, language_code, langs) if iss['severity'] == 'error']


def layer1_hints(word, language_code, senses, langs):
    """{(nghĩa, ngôn ngữ): [mô tả lỗi chắc chắn do lớp 1 tìm thấy]} để đưa vào prompt."""
    out = {}
    for i, iss in quality.check_word(senses, word, language_code, langs):
        if iss['severity'] == 'error' and iss['lang'] and iss['lang'] != language_code:
            out.setdefault((i, iss['lang']), []).append(f"{iss['code']} in {iss['field']}" + (f" ({iss['detail']})" if iss['detail'] else ''))
    return out


def evaluate(senses, langs, data, language_code='en'):
    """
    Gộp phán quyết của model thành {nghĩa: {'langs': {lang: {'state': ok|fixed|flagged, 'fixes': [...]}}, 'issues': [mô tả]}} (chưa ghi gì).
    Cặp (nghĩa, ngôn ngữ) có bản sửa thật -> fixed; không có bản sửa mà model liệt kê ổn -> ok; còn lại -> flagged.
    """
    fixes, _ = drop_noops(senses, parse_fixes(data, senses, langs, language_code))
    ok = parse_ok(data, senses, langs, language_code)
    scores = {pair: c['score'] for pair, c in parse_checks(data, senses, langs, language_code).items()}
    ok_min = getattr(settings, 'SEED_VERIFY_OK_MIN_SCORE', 80)
    out = {}
    for i, s in enumerate(senses):
        entry = {'langs': {}, 'issues': [f['value'][:300] for f in fixes if f['sense'] == i and f['field'] == 'sense_issue']}
        for lang in sense_langs(s, langs, language_code):
            mine = [f for f in fixes if f['sense'] == i and f['lang'] == lang and f['field'] != 'sense_issue']
            score = scores.get((i, lang))
            if mine:
                fix_scores = [f['score'] for f in mine if f['score'] is not None]
                state, score = 'fixed', (min(fix_scores) if fix_scores else score)
            elif (i, lang) in ok and (score is None or score >= ok_min):
                state = 'ok'
            else:
                state = 'flagged'  # im lặng, hoặc nói ổn nhưng không chắc (điểm thấp) -> để người xem
            entry['langs'][lang] = {'state': state, 'fixes': mine, 'score': score}
        out[i] = entry
    return out


def apply_to_senses(word, language_code, senses, langs, data, model=None, now=None):
    """
    Ghi kết quả vào các AISense (danh sách theo đúng thứ tự đã đưa cho model). Nghĩa verify=True (người đã kiểm) bị bỏ qua.
    Bản sửa vi phạm lớp 1 (lỗi mới) bị loại và cặp đó thành flagged. Trả {'ok', 'fixed', 'flagged', 'skipped', 'issues'}.
    """
    now = (now or timezone.now()).isoformat()
    model = model or settings.AI_REFINE_MODELS[0]
    dicts = [{'pos': s.pos, 'level': s.level, 'contents': copy.deepcopy(s.contents or {})} for s in senses]
    verdict = evaluate(dicts, langs, data, language_code)
    counts = {'ok': 0, 'fixed': 0, 'flagged': 0, 'skipped': 0, 'issues': 0}
    for i, sense in enumerate(senses):
        human = human_verified(sense)
        wanted = set(sense_langs(dicts[i], langs, language_code))
        if wanted and all(is_human(human, l) for l in wanted):
            counts['skipped'] += 1  # mọi ngôn ngữ của nghĩa đã có người kiểm
            continue
        state = dict(sense.verify_state or {})
        working = dicts[i]
        errors_before = {(iss['code'], iss['lang']) for iss in quality.check_sense(working, word, language_code, langs) if iss['severity'] == 'error'}
        for lang, result in verdict[i]['langs'].items():
            if is_human(human, lang):
                continue  # người đã kiểm ngôn ngữ này: không đụng tới nội dung lẫn trạng thái
            record = {'state': result['state'], 'ai_verify': result['state'] == 'ok', 'model': model, 'at': now}
            if result.get('score') is not None:
                record['score'] = result['score']
            if result['state'] == 'ok' and any(l == lang for _, l in errors_before):
                record.update({'state': 'flagged', 'ai_verify': False, 'note': 'model nói ổn nhưng kiểm tra lớp 1 báo lỗi'})
            fix_min = getattr(settings, 'SEED_VERIFY_FIX_MIN_SCORE', 60)
            if result['state'] == 'fixed' and result.get('score') is not None and result['score'] < fix_min:
                record.update({'state': 'flagged', 'note': 'bản sửa chưa chắc (điểm thấp), không áp dụng',
                               'proposed': [f['value'][:200] for f in result['fixes']]})
            elif result['state'] == 'fixed':
                snapshot = copy.deepcopy(working['contents'])
                count, changes = apply_fixes([working], [{**f, 'sense': 0} for f in result['fixes']], language_code)
                errors_after = {(iss['code'], iss['lang']) for iss in quality.check_sense(working, word, language_code, langs) if iss['severity'] == 'error'}
                if not count or errors_after - errors_before:
                    working['contents'] = snapshot
                    record.update({'state': 'flagged', 'note': 'bản sửa không dùng được hoặc vi phạm kiểm tra lớp 1',
                                   'proposed': [f['value'][:200] for f in result['fixes']]})
                else:
                    record['changes'] = changes
            state[lang] = record
            counts[record['state']] += 1
        if verdict[i]['issues']:
            state['_issues'] = verdict[i]['issues']
            counts['issues'] += len(verdict[i]['issues'])
        elif '_issues' in state:
            del state['_issues']
        sense.contents = working['contents']
        sense.verify_state = state
        sense.ai_verify = ai_ok_langs(state, sense_langs(working, langs, language_code))
        sense.save(update_fields=['contents', 'verify_state', 'ai_verify', 'updated_at'])
    return counts


def human_verified(sense):
    """Tập mã ngôn ngữ người đã kiểm ('*' = mọi ngôn ngữ)."""
    return set(sense.verify or [])


def is_human(human, lang):
    return '*' in human or lang in human


def ai_ok_langs(state, wanted):
    """Danh sách ngôn ngữ (trong `wanted`) mà model thứ hai đồng ý; rỗng nếu bản gốc bị báo sense_issue (tiếng Anh nghi ngờ thì chưa gì được coi là đúng)."""
    state = state or {}
    if state.get('_issues'):
        return []
    return [l for l in wanted if (state.get(l) or {}).get('ai_verify')]


def refresh_sense_flag(sense, langs, language_code='en'):
    """Tính lại ai_verify của một nghĩa từ verify_state (vd sau khi thêm ngôn ngữ mới chưa được kiểm). Không lưu."""
    sense.ai_verify = ai_ok_langs(sense.verify_state, sense_langs({'contents': sense.contents or {}}, langs, language_code))
    return sense.ai_verify
