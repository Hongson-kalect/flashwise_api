"""
Brief ảnh cho từng nghĩa (sau khi từ lưu xong): chủ thể ngắn làm khoá dùng chung giữa các ngôn ngữ (`image_subject`), câu
mô tả riêng để vẽ (`image_prompt`) và loại (`image_kind`: object / action / scene). Một lần gọi riêng cho mỗi nghĩa, tắt thinking.

Nghĩa trừu tượng / nghĩa bóng / từ ngữ pháp vẫn được vẽ thành một cảnh cụ thể dựa trên câu ví dụ (ưu tiên đồ vật / địa điểm, chỉ
dùng người khi nghĩa nói về việc người làm hoặc cảm xúc), NHƯNG khi không có tranh nào làm người học đoán được nghĩa thì model trả
`image_kind = manual` (subject / prompt để trống): nghĩa đó không vẽ, chờ người thêm ảnh tay (`check_images` liệt kê). Trước đây
mọi nghĩa bị ép phải có tranh và brief dự phòng ra ảnh gần như trống nên đã bỏ brief dự phòng. `image_prompt` không còn bắt buộc ở
schema; kind object / action / scene thì code vẫn kiểm: rỗng, quá ngắn, đòi vẽ chữ / biểu đồ, hoặc chủ thể không khớp mô tả (khoá
`inkwell` mà mô tả vẽ ngôi nhà) thì hỏi lại 1 lần kèm lời nhắc, vẫn hỏng thì coi như `manual`. Chỉ khi model không trả được JSON
nào (lỗi mạng) thì nghĩa đó vắng khỏi kết quả để lần sau viết lại.

Nhà cung cấp theo `IMAGE_BRIEF_PROVIDER`: `gemini` (mặc định, flash-lite) hoặc `claude` (Haiku). Gemini tối đa CONCURRENCY lần
cùng lúc (gọi 26 lần song song với Claude từng bị 429 vì giới hạn đồng thời).
Kết quả lưu ở `AISenseMetadata.image_metadata = {subject, prompt, kind, source}`; `source` khác nhà cung cấp hiện tại, hoặc dữ
liệu cũ có kind none, thì viết lại.
"""
import asyncio
import copy
import json
import logging
import re

from asgiref.sync import sync_to_async
from django.conf import settings

from google import genai
from google.genai import types

from utils.ai import claude, usage
from utils.ai.rate_limit import call_with_backoff

logger = logging.getLogger(__name__)

CONCURRENCY = 4
TIMEOUT = 40

RULES = """- image_subject: the plainest English phrase naming what ONE simple illustration of THIS sense shows (lowercase, 1-6 words, no articles). It is only a KEY shared by words of every language, so the same idea must get identical wording: a thing -> only its basic name, no adjectives ("refrigerator", never "white kitchen refrigerator"; "umbrella", never "open umbrella"); an action -> "person" plus the -ing verb ("person running", "person sleeping"; never child / man / woman, no place); a feeling -> "person feeling <emotion>" ("person feeling sad"). Tell homonyms apart with one extra noun ("bat animal", "baseball bat", "river bank", "money bank").
- image_prompt: ONE English sentence (8-25 words) describing exactly what the illustration looks like, for an image model that knows nothing about the word. Name the object's shape, its most recognisable parts and typical colors, and what it is used for if that avoids confusion ("a metal soup ladle: a long straight handle with a deep round bowl at the end, seen from the side"; "a house key: a small metal key with a round head and a toothed blade"; "a round-faced cartoon person with downturned eyes and a tear, looking sad"). Keep it MINIMAL: the subject plus at most ONE simple supporting prop, drawn alone and whole (nothing cropped by the frame) from a clear angle on an empty background. Never describe water, waves, splashes, ripples, ground lines, sky, scenery, motion lines or other effects. Never describe a hand, arm or any body part on its own: always the whole person or the whole object. Never include text, letters, numbers, logos, knives or other weapons held in a hand, blood, nudity, or a brand name. Choose the SIMPLEST picture, in this order: (1) one object, animal, plant or place on its own; (2) a small scene of objects and animals only; (3) a person when the sense itself is a human action, a feeling a face can show, or a person or role (boy, girl, mother, teacher, doctor, king, friend: draw that ONE simple person, whole or head and shoulders, plus at most one prop that defines the role). Never add a person to a picture of a thing, an animal, a place, weather, a quantity or a quality (heavy rain -> one dark cloud pouring rain; light in weight -> a feather; the bank of a river -> a grassy river bank). For a human action: ONE simple person (a plain stick-figure style with a round head and simple limbs is fine, no clothing or hair detail) in one clear pose that shows the action, plus the single prop that defines it (goggles for swimming, a sponge for washing); say what the limbs do in a few words (one arm raised overhead, one knee bent) rather than describing movement (kicking, splashing, rushing). A feeling or emotion a face can show (nervous, sad, angry, proud) is NOT abstract: image_kind action, image_subject "person feeling <emotion>", image_prompt a simple cartoon face and shoulders with that expression. Describe a facial expression ONLY for such feelings; otherwise a plain face.
- image_kind: object, action, scene or manual. Use manual (and leave image_subject and image_prompt empty) ONLY when no single concrete picture could make THIS meaning guessable to a learner (for example a pure grammar function word, a very abstract adverb, or a meaning that needs words to show). Never use manual for a noun, verb or adjective that names something a picture can show (a thing, animal, place, person, role, action, feeling or quality): always draw it, using a symbolic everyday picture when the meaning is abstract; a wrong or vague picture is worse than none, so do not invent one. When the sense is not a plain thing, action or feeling (abstract ideas, figurative or metaphorical senses, quantities, a liquid, a colour, a texture or a weather effect) but a concrete picture WOULD work, draw ONE concrete everyday picture that makes THIS meaning guessable, built from the usage example when one is given. Prefer a symbolic object or place over people (for example "freedom" -> an open birdcage with a bird flying out of it; "defeat" -> a toppled chess king lying beside a standing chess king; "the" -> one apple highlighted among several apples). Use a person only when the meaning is about what people do or feel. Use kind scene for these, and a literal image_subject such as "open birdcage with bird". Never use text, letters, numbers, graphs, charts, diagrams or labels to carry the meaning. Feelings that a face can show are kind action, as above."""

SYSTEM = ("You write illustration briefs for a vocabulary flashcard app. For ONE word sense, return image_kind, "
          "image_subject and image_prompt following these rules exactly:\n" + RULES)

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "image_kind": {"type": "STRING", "enum": ["object", "action", "scene", "manual"]},
        "image_subject": {"type": "STRING"},
        "image_prompt": {"type": "STRING"},
    },
    "required": ["image_kind"],
}


def sense_definition(sense):
    """Định nghĩa tiếng Anh của nghĩa (không có thì ngôn ngữ đầu tiên có); '' nếu không có."""
    definition = (sense.contents or {}).get('definition') or {}
    for lang in ('en', *definition.keys()):
        value = (definition.get(lang) or {}).get('value')
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def sense_example(sense):
    """Câu ví dụ tiếng Anh đầu tiên của nghĩa; '' nếu không có."""
    for item in (sense.contents or {}).get('examples') or []:
        value = ((item or {}).get('en') or {}).get('value') if isinstance(item, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


async def _ask_claude(user_text, system=None, schema=None, max_tokens=900):
    async def call():
        async with claude.client() as client:
            return await asyncio.wait_for(client.messages.create(
                model=settings.AI_REFINE_MODELS[0], max_tokens=max_tokens, system=system or SYSTEM,
                messages=[{'role': 'user', 'content': user_text}],
                thinking={'type': 'disabled'},
                output_config=claude.output_config(copy.deepcopy(schema or SCHEMA), effort='low'),
            ), timeout=TIMEOUT)
    response = await call_with_backoff(call, 'anthropic')
    usage.record_claude('brief', response)
    return next((b.text for b in response.content if b.type == 'text'), '')


async def _ask_gemini(user_text, system=None, schema=None, max_tokens=900):
    async def call():
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        async with client.aio as aio:
            return await asyncio.wait_for(aio.models.generate_content(
                model=settings.GEMINI_API_MODEL[0], contents=user_text,
                config=types.GenerateContentConfig(
                    system_instruction=system or SYSTEM, max_output_tokens=max_tokens, response_mime_type='application/json',
                    response_schema=copy.deepcopy(schema or SCHEMA), thinking_config=types.ThinkingConfig(thinking_budget=0)),
            ), timeout=settings.AI_GEMINI_TIMEOUT)
    response = await call_with_backoff(call, 'gemini')
    usage.record_gemini('brief', response)
    return response.text or ''


# Model nhỏ vẫn hay đòi vẽ chữ, biểu đồ, mẫu màu (FLUX vẽ chữ thành ký tự vô nghĩa, biểu đồ / mẫu màu không có hình để nhận ra):
# brief có các từ này bị coi là không vẽ được. Chặn bằng code vì chỉ dặn trong prompt thì Gemini flash-lite vẫn lọt.
# Chỉ chặn khi brief ĐÒI VẼ chữ / biểu đồ, không chặn chỉ vì có chữ "words", "text"... ("a person talking with words" vẫn vẽ được).
UNDRAWABLE_WORDS = ('says', 'saying', 'label', 'labeled', 'labelled', 'caption', 'captioned', 'speech bubble', 'speech balloon',
                    'graph', 'chart', 'diagram', 'infographic', 'notation', 'swatch')
UNDRAWABLE_PHRASES = (
    # "with the word X", "showing letters", "reading 'Open'", "written text"...
    r"\b(?:with|showing|displaying|reading|spelling|bearing|containing|writing|written|printed|marked)\b[^.]{0,25}\b(?:words?|text|letters?|numbers?|digits?)\b",
    r"\b(?:words?|text|letters?|numbers?|digits?)\b[^.]{0,12}\b(?:written|printed|on (?:it|the (?:sign|wall|paper|screen|board)))\b",
    r"['\"][^'\"]{1,30}['\"]\s*(?:sign|label|text|written)",
)


def undrawable(subject, prompt):
    haystack = f'{subject} {prompt}'.lower()
    if any(re.search(rf'\b{re.escape(word)}\b', haystack) for word in UNDRAWABLE_WORDS):
        return True
    return any(re.search(pattern, haystack) for pattern in UNDRAWABLE_PHRASES)


def provider():
    return 'claude' if settings.IMAGE_BRIEF_PROVIDER == 'claude' else 'gemini'


async def _ask(user_text):
    return await (_ask_claude if provider() == 'claude' else _ask_gemini)(user_text)


# Nhiều nghĩa của CÙNG một từ trong một lệnh: system prompt dài (~850 token) chỉ gửi một lần thay vì mỗi nghĩa một lần.
BATCH_SIZE = 8
BATCH_SYSTEM = ("You write illustration briefs for a vocabulary flashcard app. Several numbered senses of ONE word are listed. For EACH sense, "
                "return one item with n (its number), image_kind, image_subject and image_prompt following these rules exactly. "
                "Every picture must show ITS OWN sense only, never another sense of the word; write each brief on its own, do not reuse wording between senses:\n" + RULES)
BATCH_SCHEMA = {
    "type": "OBJECT",
    "properties": {"briefs": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {"n": {"type": "INTEGER"}, **SCHEMA["properties"]},
        "required": ["n", "image_kind"],
    }}},
    "required": ["briefs"],
}


async def _ask_batch(user_text):
    return await (_ask_claude if provider() == 'claude' else _ask_gemini)(user_text, system=BATCH_SYSTEM, schema=BATCH_SCHEMA, max_tokens=3500)


MIN_PROMPT_CHARS = 20
RETRY_HINT = ('\nYour previous brief was unusable (empty, too short, or it relied on text, charts or labels). '
              'Return a concrete picture of people and objects only, with no text of any kind.')
GENERIC_SUBJECTS = {'object', 'thing', 'item', 'something', 'concept', 'idea', 'symbol', 'scene', 'abstract concept'}


MANUAL = {'kind': 'manual', 'subject': '', 'prompt': ''}  # không có tranh phù hợp: không vẽ, chờ ảnh thêm tay
DRAWN_KINDS = ('object', 'action', 'scene')
_SUBJECT_STOP = {'the', 'and', 'with', 'for', 'person', 'feeling'}


def consistent(subject, prompt):
    """Chủ thể (khoá ảnh) phải xuất hiện trong mô tả: model nhỏ hay trả khoá "inkwell" mà mô tả vẽ ngôi nhà. Khớp lỏng theo gốc từ."""
    if (subject or '').lower().startswith('person '):
        return True  # "person running" / "person feeling sad": mô tả tả tư thế / nét mặt, không nhất thiết nhắc lại từ khoá
    text = (prompt or '').lower()
    tokens = [t for t in re.findall(r'[a-z]{3,}', (subject or '').lower()) if t not in _SUBJECT_STOP]
    if not tokens:
        return True  # chủ thể chỉ gồm "person feeling ..." hoặc từ chung: không đủ dữ kiện để bắt lỗi
    return any(re.sub(r'(?:es|s)$', '', t) in text for t in tokens)


def _usable(kind, subject, prompt, match=True):
    """match=False: chủ thể do code đặt thay chủ thể chung chung (không phải do model viết) nên không so với mô tả."""
    if kind == 'manual':
        return True
    return (kind in DRAWN_KINDS and bool(subject) and len(prompt) >= MIN_PROMPT_CHARS
            and not undrawable(subject, prompt) and (not match or consistent(subject, prompt)))


def _result(kind, subject, prompt):
    return dict(MANUAL) if kind == 'manual' else {'kind': kind, 'subject': subject, 'prompt': prompt[:400]}


async def make_brief(word, pos, definition, example='', ask=None):
    """
    {kind, subject, prompt} cho 1 nghĩa: AI trả hỏng thì hỏi lại 1 lần, vẫn hỏng thì kind `manual` (không vẽ, chờ ảnh tay).
    None khi cả hai lần đều không có JSON đọc được (lỗi mạng / model): chưa kết luận gì, lần sau viết lại.
    """
    answered = False
    user = f'Word: {word} ({pos or "?"})\nSense: {definition}' + (f'\nUsage example: {example}' if example else '')
    for attempt in range(2):
        try:
            data = json.loads(await (ask or _ask)(user + (RETRY_HINT if attempt else '')))
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        answered = True
        kind = data.get('image_kind')
        subject = (data.get('image_subject') or '').strip().lower()
        prompt = (data.get('image_prompt') or '').strip()
        generic = subject in GENERIC_SUBJECTS
        if generic:
            subject = f'{word.strip().lower()} {(pos or "").strip().lower()}'.strip()
        if _usable(kind, subject, prompt, match=not generic):
            return _result(kind, subject, prompt)
    return dict(MANUAL) if answered else None


def _clean_brief(word, pos, data):
    """Dữ liệu AI cho 1 nghĩa -> brief dùng được, hoặc None."""
    kind = data.get('image_kind')
    subject = (data.get('image_subject') or '').strip().lower()
    prompt = (data.get('image_prompt') or '').strip()
    generic = subject in GENERIC_SUBJECTS
    if generic:
        subject = f'{word.strip().lower()} {(pos or "").strip().lower()}'.strip()
    if _usable(kind, subject, prompt, match=not generic):
        return _result(kind, subject, prompt)
    return None


def brief_chunks(word, items):
    """Chia nghĩa của MỘT từ thành các lệnh gộp (tối đa BATCH_SIZE nghĩa): yield (nhóm nghĩa, nội dung hỏi). Dùng chung cho lệnh online và batch."""
    for start in range(0, len(items), BATCH_SIZE):
        chunk = items[start:start + BATCH_SIZE]
        lines = [f"{i}. ({it.get('pos') or '?'}) {it['definition']}" + (f" | Usage example: {it['example']}" if it.get('example') else '')
                 for i, it in enumerate(chunk, 1)]
        yield chunk, f'Word: {word}\nSenses:\n' + '\n'.join(lines)


def parse_chunk_briefs(word, chunk, data):
    """Kết quả JSON của một lệnh gộp -> ({key: brief dùng được}, [nghĩa thiếu / hỏng])."""
    rows = {r.get('n'): r for r in ((data or {}).get('briefs') or []) if isinstance(r, dict)}
    out, missing = {}, []
    for i, it in enumerate(chunk, 1):
        cleaned = _clean_brief(word, it.get('pos'), rows.get(i) or {})
        if cleaned:
            out[it['key']] = cleaned
        else:
            missing.append(it)
    return out, missing


async def make_briefs(word, items, ask_batch=None, ask=None):
    """
    Brief cho nhiều nghĩa của MỘT từ: tối đa BATCH_SIZE nghĩa mỗi lệnh. items: [{'key', 'pos', 'definition', 'example'}] -> {key: brief}.
    Nghĩa nào thiếu / hỏng trong kết quả (hoặc cả lệnh lỗi) thì hỏi lại riêng từng nghĩa bằng make_brief (hỏng nữa thì `manual`).
    Lỗi 1 nghĩa chỉ ghi log, nghĩa đó vắng khỏi kết quả.
    """
    out, singles = {}, []
    if len(items) <= 1:
        singles = list(items)
    for chunk, question in (brief_chunks(word, items) if len(items) > 1 else ()):
        try:
            data = json.loads(await (ask_batch or _ask_batch)(question))
            data.get('briefs')
        except Exception as e:
            logger.warning('[IMAGE-BRIEF] Lệnh gộp cho "%s" lỗi, hỏi riêng từng nghĩa: %s', word, e)
            singles.extend(chunk)
            continue
        made, missing = parse_chunk_briefs(word, chunk, data)
        out.update(made)
        singles.extend(missing)
    for it in singles:
        try:
            made = await make_brief(word, it.get('pos'), it['definition'], example=it.get('example') or '', ask=ask)
            if made:
                out[it['key']] = made
        except Exception as e:
            logger.warning('[IMAGE-BRIEF] Brief cho "%s" (%s) lỗi: %s', word, it['key'], e)
    return out


def _text(value):
    return value.get('value') if isinstance(value, dict) else value


def draft_fields(sense_obj, language_code):
    """(định nghĩa, loại từ, câu ví dụ đầu) của nghĩa đang dựng (cấu trúc của word_render: contents.definition[lang] là chuỗi)."""
    contents = sense_obj.get('contents') or {}
    definition = _text((contents.get('definition') or {}).get(language_code)) or ''
    example = ''
    for item in contents.get('examples') or []:
        value = _text(item.get(language_code)) if isinstance(item, dict) else None
        if isinstance(value, str) and value.strip():
            example = value.strip()
            break
    return (definition.strip() if isinstance(definition, str) else ''), (sense_obj.get('pos') or ''), example


async def draft_briefs(word_value, language_code, sense_objs, ask=None, ask_batch=None):
    """
    Brief cho các nghĩa NHÁP, chạy song song với bước refine (refine mất ~7s, brief ~1s). Trả
    {sense_id: {'sig': (định nghĩa, loại từ), 'brief': brief}}. Lỗi từng nghĩa chỉ ghi log (nghĩa đó sẽ được viết sau khi lưu).
    """
    if ask is None or ask_batch is not None:  # chạy thật: gộp mọi nghĩa của từ vào ít lệnh nhất
        fields = {x['id']: draft_fields(x, language_code) for x in sense_objs if x.get('id')}
        items = [{'key': k, 'pos': f[1], 'definition': f[0], 'example': f[2]} for k, f in fields.items() if f[0]]
        made = await make_briefs(word_value, items, ask_batch=ask_batch, ask=ask)
        return {k: {'sig': (fields[k][0], fields[k][1]), 'brief': b} for k, b in made.items()}
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(sense):
        definition, pos, example = draft_fields(sense, language_code)
        if not definition or not sense.get('id'):
            return None
        async with gate:
            try:
                made = await make_brief(word_value, pos, definition, example=example, ask=ask)
            except Exception as e:
                logger.warning('[IMAGE-BRIEF] Brief nháp cho "%s" lỗi: %s', word_value, e)
                return None
        return sense['id'], {'sig': (definition, pos), 'brief': made}

    return dict(x for x in await asyncio.gather(*[one(s) for s in sense_objs]) if x)


def reusable_briefs(drafts, final_sense_objs, language_code, strict=True):
    """
    Sau refine: chỉ giữ brief của nghĩa CÒN LẠI mà định nghĩa + loại từ không đổi. Nghĩa bị xoá thì mất, nghĩa bị sửa / mới thêm
    không có trong kết quả (sẽ được viết lại sau khi lưu).
    strict=False (chế độ vẽ trước refine): refine hay chỉ trau chuốt câu chữ định nghĩa nên chỉ cần loại từ không đổi.
    """
    out = {}
    for sense in final_sense_objs:
        entry = (drafts or {}).get(sense.get('id'))
        if entry and (draft_fields(sense, language_code)[:2] == entry['sig'] if strict else draft_fields(sense, language_code)[1] == entry['sig'][1]):
            out[sense['id']] = entry['brief']
    return out


def needs_brief(sense):
    meta = sense.metadata.image_metadata if sense.metadata_id and sense.metadata else None
    meta = meta or {}
    return sense.metadata_id is not None and (meta.get('source') != provider() or meta.get('kind') not in (*DRAWN_KINDS, 'manual'))


async def ensure_briefs(senses, ask=None, ask_batch=None):
    """Viết brief còn thiếu cho các nghĩa (lỗi từng nghĩa chỉ ghi log, nghĩa đó bị bỏ qua lần này). Trả số nghĩa đã viết."""
    todo = [s for s in senses if needs_brief(s) and sense_definition(s)]
    if not todo:
        return 0
    if ask is None or ask_batch is not None:  # chạy thật: gộp theo từ
        by_word = {}
        for sense in todo:
            by_word.setdefault(sense.word_id, []).append(sense)
        word_gate = asyncio.Semaphore(CONCURRENCY)

        async def one_word(group):
            items = [{'key': str(x.id), 'pos': x.pos, 'definition': sense_definition(x), 'example': sense_example(x)} for x in group]
            async with word_gate:
                made = await make_briefs(group[0].word_value, items, ask_batch=ask_batch, ask=ask)
            saved = 0
            for x in group:
                brief = made.get(str(x.id))
                if brief:
                    meta = x.metadata
                    meta.image_metadata = {**(meta.image_metadata or {}), 'subject': brief['subject'], 'prompt': brief['prompt'],
                                           'kind': brief['kind'], 'source': provider()}
                    await sync_to_async(meta.save)(update_fields=['image_metadata'])
                    saved += 1
            return saved

        return sum(await asyncio.gather(*[one_word(g) for g in by_word.values()]))
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(sense):
        async with gate:
            try:
                brief = await make_brief(sense.word_value, sense.pos, sense_definition(sense), example=sense_example(sense), ask=ask)
            except Exception as e:
                logger.warning('[IMAGE-BRIEF] Brief cho "%s" (%s) lỗi: %s', sense.word_value, sense.id, e)
                return 0
        meta = sense.metadata
        meta.image_metadata = {**(meta.image_metadata or {}), 'subject': brief['subject'], 'prompt': brief['prompt'],
                               'kind': brief['kind'], 'source': provider()}
        await sync_to_async(meta.save)(update_fields=['image_metadata'])
        return 1

    return sum(await asyncio.gather(*[one(s) for s in todo]))
