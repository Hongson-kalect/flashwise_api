"""
Bước 2 của pipeline tạo từ: chuẩn hoá bản draft.

Bước 1 (word_render.py) stream từng nghĩa về cho người tra đầu tiên ngay khi model rẻ trả ra.
Bước 2 gửi toàn bộ draft cho Claude (tắt thinking) để kiểm tra và chuẩn hoá, kết quả này mới được ghi vào DB
(draft không bao giờ vào DB). Bản dịch / metadata sinh từ draft chỉ làm lại cho phần bị sửa.

Phần thuần (prompt, apply_refinement, build_translate_jobs) không gọi mạng nên test được trực tiếp.
"""
import asyncio
import copy
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone as dt_timezone

from django.conf import settings

from utils.ai import claude
from utils.ai.constain import language_map
from utils.ai.prompt import render_reference
from utils.ai.romanize import ROMAN_LANGS, add_readings, reading_ipas
from utils.utils import uuidv7
from utils.ai import usage
from utils.utils.best_effort import best_effort
from utils.utils.socket import socket_message

logger = logging.getLogger(__name__)

REGISTER_VALUES = ["neutral", "formal", "informal", "slang", "vulgar", "technical", "literary", "archaic", "dialect", "humorous"]
LEVEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 \-]{0,19}$')

# AISense.pos, AISense.level là CharField(max_length=20)
MAX_POS = 20
MAX_LEVEL = 20

refine_schema = {
    "type": "OBJECT",
    "properties": {
        "senses": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING", "description": "Copied exactly for keep / fix / remove. Omit for add."},
                    "action": {"type": "STRING", "enum": ["keep", "fix", "remove", "add"]},
                    "reason": {"type": "STRING", "description": "Why it was fixed, removed or added. Empty for keep."},
                    # fix: chỉ điền các trường thật sự thay đổi. add: điền đủ pos, definition, examples (và usage, level, register, ipas nếu có)
                    "pos": {"type": "STRING"},
                    "definition": {"type": "STRING"},
                    "usage": {"type": "STRING"},
                    "examples": {"type": "ARRAY", "items": {"type": "STRING"}, "maxItems": 2},
                    "level": {"type": "STRING"},
                    "register": {"type": "ARRAY", "items": {"type": "STRING", "enum": REGISTER_VALUES}},
                    "ipas": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {"value": {"type": "STRING"}, "label": {"type": "STRING"}},
                            "required": ["value", "label"],
                        },
                    },
                    "is_offensive": {"type": "BOOLEAN"},
                },
                "required": ["action"],
            },
        },
    },
    "required": ["senses"],
}


# ======================= PHẦN THUẦN =======================

def _text(node):
    """Chuỗi trong node {"value": "..."} (hoặc chuỗi thường), rỗng nếu không có."""
    if isinstance(node, dict):
        node = node.get('value')
    return node.strip() if isinstance(node, str) else ''


def _same(a, b):
    """Hai chuỗi coi như một nếu chỉ khác dạng chữ Unicode (full / half width...) hoặc khoảng trắng: không bắt dịch lại vì sửa vặt."""
    norm = lambda t: ' '.join(unicodedata.normalize('NFKC', t).split())
    return norm(a) == norm(b)


def _clean(value, max_len):
    return value.strip()[:max_len] if isinstance(value, str) else ''


def draft_view(sense, language_code):
    """Dạng gọn của 1 nghĩa draft để đưa vào prompt (chỉ nội dung ngôn ngữ gốc, không có bản dịch)."""
    contents = sense.get('contents') or {}
    return {
        'id': sense['id'],
        'pos': sense.get('pos'),
        'definition': _text((contents.get('definition') or {}).get(language_code)),
        'usage': _text((contents.get('usage') or {}).get(language_code)),
        'examples': [_text(ex.get(language_code)) for ex in contents.get('examples') or [] if isinstance(ex, dict)],
        'level': sense.get('level'),
        'register': sense.get('register') or [],
        'ipas': [{'value': i.get('value'), 'label': i.get('label')} for i in sense.get('ipas') or [] if isinstance(i, dict)],
        'is_offensive': bool(sense.get('is_offensive')),
    }


REFINE_SYSTEM_PROMPT = (
    "You are a senior lexicographer reviewing a junior's draft entry for a learner's dictionary. "
    "You correct real errors and leave correct content untouched."
)


def render_refine_prompt(word, language_code, senses, reference=None):
    """Prompt cho Claude Haiku: dữ liệu trong thẻ XML, quy tắc ngắn có lý do (model làm theo tốt hơn khi biết vì sao)."""
    language_name = language_map.get(language_code, 'en')
    drafts = json.dumps([draft_view(s, language_code) for s in senses], ensure_ascii=False)
    if settings.AI_REFINE_ALLOW_ADD:
        add_rule = f"7. Missing senses: add a common, distinct meaning a learner will meet only if the draft clearly lacks it (at most {settings.AI_REFINE_MAX_ADD}). No rare, archaic, technical or idiom-only meanings, no near-duplicates."
        add_output = "\n- action=add (no id) for each missing sense: pos, definition, 1-2 examples, plus usage / level / register / ipas when useful, and a short reason."
    else:
        add_rule = "7. Never add new senses; only review the ones given."
        add_output = ""
    if language_code == 'ja':
        # ipas tiếng Nhật là cách đọc hiragana (label READING), không phải IPA: không để model "sửa" thành romaji
        ipa_check = "ipas: label READING holds the full hiragana reading of the word; keep it in hiragana."
    else:
        ipa_check = "ipas: valid IPA between slashes, matching the word and its label."

    reference_text = render_reference(reference)
    reference_block = f"""

<reference>
{reference_text}
</reference>""" if reference_text else ""
    reference_check = """
8. Reference: entries from another dictionary, unverified and unordered, only a second opinion. A draft sense or pos that neither your own knowledge nor the reference supports is probably invented: remove it. Do not copy the reference's wording, and do not fix a draft that is correct just because the reference words it differently.""" if reference_text else ""

    return f"""Review the draft senses of the {language_name} word '{word}'. All content is in {language_name}.

<draft>
{drafts}
</draft>{reference_block}

Change only what is wrong. Each change is re-translated into several languages, so leave correct text exactly as it is: do not reword, shorten or polish it.

<checks>
1. Meaning: the definition is accurate for its pos and is a real meaning of '{word}'. Remove invented or wrong senses. If a sense blends two unrelated meanings, fix it to the more common one.
2. Duplicates: remove a sense with the same meaning and pos as another (keep the better one). Check again after your own fixes.
3. Form: pos is a lowercase English term (noun, verb, adjective, ...; a noun used before another noun stays a noun); level is CEFR A1-C2 (JLPT N5-N1 for Japanese, TOPIK1-6 for Korean); definition is one clear sentence that does not use '{word}' itself.
4. usage: only a short note on grammar, prepositions / particles, collocations or register, about this sense only. If it holds an example sentence, is wrong, or describes another sense, rewrite it or return "" to delete it.
5. examples: at most 2, natural and grammatical, each containing '{word}' or an inflected form and showing this sense, not another one. Replace an example that fits another sense.
6. {ipa_check}
{add_rule}{reference_check}
</checks>

<output>
One item per draft sense, with its id copied exactly:
- action=keep: nothing to change, no other fields.
- action=fix: only the changed fields (examples: the full corrected list), plus a short reason.
- action=remove: a short reason.{add_output}
</output>"""


def _apply_fix(sense, item, language_code):
    """Áp 1 bản sửa lên nghĩa (sửa tại chỗ). Trả về mô tả thay đổi, None nếu không có gì đổi."""
    contents = sense.setdefault('contents', {})
    fields = set()
    scope = {'definition': False, 'usage': False, 'examples': []}

    pos = _clean(item.get('pos'), MAX_POS).lower()
    if pos and pos != sense.get('pos'):
        sense['pos'] = pos
        fields.add('pos')

    definition = _clean(item.get('definition'), 1000)
    if definition and not _same(definition, _text((contents.get('definition') or {}).get(language_code))):
        # Bản dịch cũ gắn với nghĩa cũ -> bỏ, dịch lại từ nghĩa mới
        contents['definition'] = {language_code: {'value': definition}}
        contents.pop('translations', None)
        fields.add('definition')
        scope['definition'] = True

    usage = _clean(item.get('usage'), 1000)
    if not usage and isinstance(item.get('usage'), str) and _text((contents.get('usage') or {}).get(language_code)):
        # Model chủ động trả chuỗi rỗng: usage cũ không phải ghi chú ngữ pháp (thường là câu ví dụ) -> bỏ luôn, kể cả bản dịch của nó
        contents.pop('usage', None)
        fields.add('usage')
    elif usage and not _same(usage, _text((contents.get('usage') or {}).get(language_code))):
        contents['usage'] = {language_code: {'value': usage}}
        fields.add('usage')
        scope['usage'] = True

    if isinstance(item.get('examples'), list):
        new_texts = [t for t in (_clean(x, 500) for x in item['examples']) if t][:2]
        old_nodes = contents.get('examples') or []
        old_texts = [_text(ex.get(language_code)) if isinstance(ex, dict) else '' for ex in old_nodes]
        if new_texts and not (len(new_texts) == len(old_texts) and all(_same(n, o) for n, o in zip(new_texts, old_texts))):
            nodes = []
            for i, text in enumerate(new_texts):
                if i < len(old_nodes) and _same(old_texts[i], text):
                    nodes.append(old_nodes[i])  # câu không đổi giữ nguyên bản dịch
                else:
                    nodes.append({language_code: {'value': text}})
                    scope['examples'].append(i)
            contents['examples'] = nodes
            fields.add('examples')

    level = _clean(item.get('level'), MAX_LEVEL).upper()
    if level and LEVEL_RE.match(level) and level != sense.get('level'):
        sense['level'] = level
        fields.add('level')

    if isinstance(item.get('register'), list):
        register = list(dict.fromkeys(r for r in item['register'] if r in REGISTER_VALUES))
        if register and register != (sense.get('register') or []):
            sense['register'] = register
            fields.add('register')

    if isinstance(item.get('ipas'), list):
        ipas = [
            {'value': _clean(i.get('value'), 100), 'label': _clean(i.get('label'), 20)}
            for i in item['ipas'] if isinstance(i, dict) and _clean(i.get('value'), 100)
        ]
        if ipas and language_code in ROMAN_LANGS:
            ipas = reading_ipas(sense.get('word_value'), language_code, ipas) or ipas  # cách đọc + roman do server dựng
        if ipas and ipas != (sense.get('ipas') or []):
            sense['ipas'] = ipas
            fields.add('ipas')

    if isinstance(item.get('is_offensive'), bool) and item['is_offensive'] != bool(sense.get('is_offensive')):
        sense['is_offensive'] = item['is_offensive']
        fields.add('is_offensive')

    if not fields:
        return None
    if language_code in ROMAN_LANGS and ({'definition', 'usage', 'examples'} & fields):
        add_readings(contents, language_code)
    return {'fields': fields, 'scope': scope, 'reason': _clean(item.get('reason'), 300)}


def _new_sense(item, template, language_code, existing):
    """Dựng nghĩa mới từ item action=add, cùng dạng với nghĩa của bước 1. None nếu thiếu / trùng nghĩa đã có."""
    pos = _clean(item.get('pos'), MAX_POS).lower()
    definition = _clean(item.get('definition'), 1000)
    examples = [t for t in (_clean(x, 500) for x in item.get('examples') or []) if t][:2] if isinstance(item.get('examples'), list) else []
    if not pos or not definition or not examples:
        return None
    for old in existing:
        if old.get('pos') == pos and _same(definition, _text(((old.get('contents') or {}).get('definition') or {}).get(language_code))):
            return None

    usage = _clean(item.get('usage'), 1000)
    contents = {
        'definition': {language_code: {'value': definition}},
        'examples': [{language_code: {'value': text}} for text in examples],
    }
    if usage:
        contents['usage'] = {language_code: {'value': usage}}

    level = _clean(item.get('level'), MAX_LEVEL).upper()
    register = [r for r in dict.fromkeys(item.get('register') or []) if r in REGISTER_VALUES] if isinstance(item.get('register'), list) else []
    ipas = [
        {'value': _clean(i.get('value'), 100), 'label': _clean(i.get('label'), 20)}
        for i in item.get('ipas') or [] if isinstance(i, dict) and _clean(i.get('value'), 100)
    ] if isinstance(item.get('ipas'), list) else []
    sense = {
        'id': str(uuidv7.generate_uuid7()),
        'word_id': template.get('word_id'),
        'word_value': template.get('word_value'),
        'language_code': language_code,
        'is_offensive': bool(item.get('is_offensive')),
        'pos': pos,
        'level': level if LEVEL_RE.match(level) else None,
        'register': register or ['neutral'],
        'ipas': (reading_ipas(template.get('word_value'), language_code, ipas or template.get('ipas')) if language_code in ROMAN_LANGS
                 else ipas or (template.get('ipas') or [])),  # cùng từ nên phát âm giống các nghĩa có sẵn
        'contents': contents,
    }
    if language_code in ROMAN_LANGS:
        add_readings(contents, language_code)
    return sense


def apply_refinement(senses, refinement, language_code):
    """
    Áp kết quả refine lên danh sách nghĩa draft (sửa tại chỗ).
    - id lạ bị bỏ qua; nghĩa model không nhắc tới coi như giữ nguyên.
    - action=add dựng nghĩa mới (tối đa AI_REFINE_MAX_ADD, thiếu trường hoặc trùng nghĩa có sẵn thì bỏ).
    - Model đòi xoá hết mà không bổ sung nghĩa nào thì bỏ qua yêu cầu xoá (không bao giờ để từ không còn nghĩa).
    Trả (các nghĩa còn lại, report); report['added'] là nghĩa mới, đã nằm cuối danh sách trả về.
    """
    report = {'removed': [], 'changed': [], 'added': [], 'scope': {}, 'all_removed_ignored': False}
    items = refinement.get('senses') if isinstance(refinement, dict) else None
    if not isinstance(items, list):
        return senses, report

    by_id = {s['id']: s for s in senses}
    removals = {}
    changes = {}
    added = []

    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get('action') == 'add':
            if settings.AI_REFINE_ALLOW_ADD and senses and len(added) < settings.AI_REFINE_MAX_ADD:
                new = _new_sense(item, senses[0], language_code, [*senses, *(sense for sense, _ in added)])
                if new:
                    added.append((new, _clean(item.get('reason'), 300)))
            continue
        sense = by_id.get(item.get('id'))
        if sense is None:
            continue
        action = item.get('action')
        if action == 'remove':
            removals[sense['id']] = _clean(item.get('reason'), 300)
        elif action == 'fix':
            change = _apply_fix(sense, item, language_code)
            if change:
                changes[sense['id']] = change

    if removals and len(removals) >= len(senses) and not added:
        report['all_removed_ignored'] = True
        removals = {}

    survivors = [s for s in senses if s['id'] not in removals]
    for new, reason in added:
        survivors.append(new)
        report['added'].append({'id': new['id'], 'reason': reason})
        # Nghĩa mới chưa có bản dịch nào: dịch toàn bộ phần có nội dung
        report['scope'][new['id']] = {
            'definition': True,
            'usage': 'usage' in new['contents'],
            'examples': list(range(len(new['contents']['examples']))),
        }
    report['removed'] = [{'id': sid, 'reason': reason} for sid, reason in removals.items()]
    for sid, change in changes.items():
        if sid in removals:
            continue
        report['changed'].append({'id': sid, 'fields': sorted(change['fields']), 'reason': change['reason']})
        report['scope'][sid] = change['scope']
    return survivors, report


def build_translate_jobs(senses, report, language_code):
    """
    Danh sách job dịch lại cho ai_create_translate_sema, chỉ gồm phần nội dung bị sửa:
    definition / usage / từng example (index 1-based như translate.py), translations chỉ dịch lại khi definition đổi.
    """
    by_id = {s['id']: s for s in senses}
    jobs = []
    for sid, scope in report['scope'].items():
        sense = by_id.get(sid)
        if sense is None:
            continue
        contents = sense['contents']
        job = {}
        if scope['definition']:
            job['definition'] = {language_code: contents['definition'][language_code]}
        if scope['usage']:
            job['usage'] = {language_code: contents['usage'][language_code]}
        if scope['examples']:
            job['examples'] = [{'index': i + 1, language_code: contents['examples'][i][language_code]} for i in scope['examples']]
        if not job:
            continue
        job['translations'] = contents['definition'][language_code]['value'] if scope['definition'] else False
        jobs.append({'id': sid, 'contents': job})
    return jobs


AI_FIELDS = ('quality_status', 'ai_pass', 'ai_model', 'ai_review')


def public_sense(sense):
    """Nghĩa để gửi client / cache: bỏ các trường nội bộ ghi dấu nguồn gốc AI."""
    return {k: v for k, v in sense.items() if k not in AI_FIELDS}


def stamp_senses(senses, report, model):
    """Ghi dấu nguồn gốc lên từng nghĩa trước khi lưu DB (các trường này có trên AISense)."""
    changed = {c['id']: c for c in report['changed']}
    added = {a['id']: a for a in report.get('added', [])}
    at = datetime.now(dt_timezone.utc).isoformat()
    for sense in senses:
        change = changed.get(sense['id'])
        sense['quality_status'] = 'refined'
        sense['ai_pass'] = 2
        sense['ai_model'] = model
        if sense['id'] in added:
            sense['ai_review'] = {'added': True, 'reason': added[sense['id']]['reason'], 'at': at}
        else:
            sense['ai_review'] = {'fields': change['fields'], 'reason': change['reason'], 'at': at} if change else {'unchanged': True, 'at': at}


# ======================= PHẦN GỌI MẠNG =======================

async def request_refinement(prompt):
    """
    Gọi Claude (tắt thinking, độ kỹ AI_REFINE_EFFORT), thử lần lượt AI_REFINE_MODELS.
    Trả {'data': dict, 'model': str}, hoặc None nếu mọi model đều lỗi / quá hạn.
    """
    from utils.ai.rate_limit import is_rate_limited, wait_before_retry

    models = list(settings.AI_REFINE_MODELS)
    limited = 0
    while models:
        model = models[0]
        try:
            async with claude.client() as client:
                response = await asyncio.wait_for(
                    client.messages.create(
                        model=model,
                        max_tokens=8192,
                        system=REFINE_SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": prompt}],
                        thinking={"type": "disabled"},  # mọi bước đều tắt thinking; Haiku chỉ nhận disabled khi effort <= high
                        output_config=claude.output_config(refine_schema, effort=settings.AI_REFINE_EFFORT),
                    ),
                    timeout=settings.AI_REFINE_TIMEOUT,
                )
            usage.record_claude('refine', response)
            if response.stop_reason in ('refusal', 'max_tokens'):
                logger.warning('Refine %s dừng vì %s', model, response.stop_reason)
                models.pop(0)
                continue
            text = next((block.text for block in response.content if block.type == 'text'), '')
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get('senses'), list):
                return {'data': data, 'model': model}
            logger.warning('Refine %s trả về sai cấu trúc', model)
        except Exception as e:
            logger.warning('Refine lỗi với %s: %s', model, e)
            # Hết hạn mức thì chờ rồi thử lại cùng model (quá hạn thời gian thì không chờ, sang model kế tiếp)
            if is_rate_limited(e):
                limited += 1
                if await wait_before_retry(e, limited, 'anthropic'):
                    continue
        models.pop(0)
    return None


async def refine_word(word_value, language_code, senses, refinement, *, draft_model=None, socket_room=None, cache=None, base_languages=None):
    """
    Áp kết quả refine, làm lại bản dịch / metadata cho phần đã sửa, báo socket cho người đang chờ.
    Không bao giờ ném lỗi: refine hỏng thì giữ nguyên draft (quality_status = draft) để từ vẫn được lưu.
    Trả về các nghĩa sẽ ghi vào DB.
    """
    # import muộn: translate / metadata kéo theo nhiều phụ thuộc, test phần thuần không cần
    from utils.ai.metadata import ai_create_metadata
    from utils.ai.translate import ai_create_translate_sema

    pristine = copy.deepcopy(senses)  # apply_refinement sửa tại chỗ; lỗi giữa chừng thì phải quay về đúng bản draft, không để nửa refined nửa draft

    async def notify(payload):
        try:
            await socket_message(socket_room, payload)
        except Exception as e:
            logger.warning('Gửi socket %s lỗi, bỏ qua: %s', payload.get('type'), e)

    def keep_draft():
        for sense in pristine:
            sense['quality_status'] = 'draft'
            sense['ai_pass'] = 1
            sense['ai_model'] = draft_model
        return pristine

    if not refinement:
        return keep_draft()

    try:
        survivors, report = apply_refinement(senses, refinement['data'], language_code)
        stamp_senses(survivors, report, refinement['model'])
        by_id = {s['id']: s for s in survivors}

        if socket_room and report['removed']:
            await notify({"type": "SENSE_REMOVED", "payload": [r['id'] for r in report['removed']]})

        if report['changed'] or report['added']:
            payload = {}
            for change in [*report['changed'], *report['added']]:
                sense = by_id[change['id']]
                payload[sense['id']] = {k: sense.get(k) for k in ('id', 'pos', 'level', 'register', 'ipas', 'is_offensive', 'contents')}
            if socket_room:
                # App gộp theo id: nghĩa đã gửi ở bước 1 được thay bằng bản chuẩn hoá, nghĩa mới (id lạ) được thêm vào
                await notify({"type": "PARTIAL_SENSE", "payload": payload})

            jobs = build_translate_jobs(survivors, report, language_code)
            langs = ((best_effort(cache.cache_word_get_data, language_code, word_value) if cache else None) or {}).get('langs') or [language_code]
            tasks = []
            if jobs:
                tasks.append(ai_create_translate_sema({
                    "word_value": word_value,
                    "language_code": language_code,
                    "user_language_code": langs,
                    "missing_translate": jobs,
                    "current_senses": [by_id[j['id']] for j in jobs],
                    "base_languages": base_languages,
                }))
            # Metadata (đồng nghĩa, thành ngữ, từ khoá ảnh...) sinh từ nghĩa + loại từ nên chỉ làm lại khi một trong hai đổi
            meta_senses = [by_id[c['id']] for c in report['changed'] if {'definition', 'pos'} & set(c['fields'])] + [by_id[a['id']] for a in report['added']]
            if meta_senses:
                tasks.append(ai_create_metadata({
                    "word_value": word_value,
                    "language_code": language_code,
                    "current_senses": meta_senses,
                }))
            if tasks:
                try:
                    # Cũng có trần thời gian như bước chờ dịch của bước 1: lệnh gọi treo không giữ người dùng chờ vô hạn
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=settings.AI_BACKGROUND_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning('Refine %s: dịch / metadata lại quá %ss, lưu phần đã có', word_value, settings.AI_BACKGROUND_TIMEOUT)

        logger.info('Refine %s: giữ %s, sửa %s, xoá %s, thêm %s', word_value, len(survivors) - len(report['changed']) - len(report['added']), len(report['changed']), len(report['removed']), len(report['added']))
        return survivors
    except Exception:
        logger.exception('Refine %s lỗi khi áp kết quả, giữ bản draft', word_value)
        return keep_draft()
