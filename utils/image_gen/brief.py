"""
Brief ảnh cho từng nghĩa (sau khi từ lưu xong): chủ thể ngắn làm khoá dùng chung giữa các ngôn ngữ (`image_subject`), câu
mô tả riêng để vẽ (`image_prompt`) và loại (`image_kind`: object / action / scene). Một lần gọi riêng cho mỗi nghĩa, tắt thinking.

MỌI nghĩa đều phải có tranh (không còn `none`): nghĩa trừu tượng / nghĩa bóng / từ ngữ pháp được vẽ thành một cảnh người que cụ
thể dựa trên câu ví dụ (ưu tiên đồ vật / địa điểm, chỉ dùng người khi nghĩa nói về việc người làm hoặc cảm xúc). `image_prompt` bắt buộc (schema required + kiểm tra ở code): rỗng, quá ngắn hoặc đòi vẽ chữ / biểu đồ thì hỏi
lại 1 lần kèm lời nhắc, vẫn hỏng thì dùng brief dự phòng dựng từ chính từ và định nghĩa.

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
- image_prompt: ONE English sentence (8-25 words) describing exactly what the illustration looks like, for an image model that knows nothing about the word. Name the object's shape, its most recognisable parts and typical colors, and what it is used for if that avoids confusion ("a metal soup ladle: a long straight handle with a deep round bowl at the end, seen from the side"; "a house key: a small metal key with a round head and a toothed blade"; "a round-faced cartoon person with downturned eyes and a tear, looking sad"). Keep it MINIMAL: the subject plus at most ONE simple supporting prop, drawn alone and whole (nothing cropped by the frame) from a clear angle on an empty background. Never describe water, waves, splashes, ripples, ground lines, sky, scenery, motion lines or other effects. Never describe a hand, arm or any body part on its own: always the whole person or the whole object. Never include text, letters, numbers, logos, knives or other weapons held in a hand, blood, nudity, or a brand name. Choose the SIMPLEST picture, in this order: (1) one object, animal, plant or place on its own; (2) a small scene of objects and animals only; (3) a person ONLY when the sense itself is a human action or a feeling a face can show. Never add a person to a picture of a thing, an animal, a place, weather, a quantity or a quality (heavy rain -> one dark cloud pouring rain; light in weight -> a feather; the bank of a river -> a grassy river bank). For a human action: ONE simple person (a plain stick-figure style with a round head and simple limbs is fine, no clothing or hair detail) in one clear pose that shows the action, plus the single prop that defines it (goggles for swimming, a sponge for washing); say what the limbs do in a few words (one arm raised overhead, one knee bent) rather than describing movement (kicking, splashing, rushing). A feeling or emotion a face can show (nervous, sad, angry, proud) is NOT abstract: image_kind action, image_subject "person feeling <emotion>", image_prompt a simple cartoon face and shoulders with that expression. Describe a facial expression ONLY for such feelings; otherwise a plain face.
- image_kind: object, action or scene. EVERY sense MUST get a picture, never refuse. When the sense is not a plain thing, action or feeling (grammar words, abstract ideas, figurative or metaphorical senses, quantities, a liquid, a colour, a texture or a weather effect), draw ONE concrete everyday picture that makes THIS meaning guessable, built from the usage example when one is given. Prefer a symbolic object or place over people (for example "freedom" -> an open birdcage with a bird flying out of it; "defeat" -> a toppled chess king lying beside a standing chess king; "the" -> one apple highlighted among several apples). Use a person only when the meaning is about what people do or feel. Use kind scene for these, and a literal image_subject such as "open birdcage with bird". Never use text, letters, numbers, graphs, charts, diagrams or labels to carry the meaning. Feelings that a face can show are kind action, as above."""

SYSTEM = ("You write illustration briefs for a vocabulary flashcard app. For ONE word sense, return image_kind, "
          "image_subject and image_prompt following these rules exactly:\n" + RULES)

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "image_kind": {"type": "STRING", "enum": ["object", "action", "scene"]},
        "image_subject": {"type": "STRING"},
        "image_prompt": {"type": "STRING"},
    },
    "required": ["image_kind", "image_subject", "image_prompt"],
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


async def _ask_claude(user_text):
    async def call():
        async with claude.client() as client:
            return await asyncio.wait_for(client.messages.create(
                model=settings.AI_REFINE_MODELS[0], max_tokens=900, system=SYSTEM,
                messages=[{'role': 'user', 'content': user_text}],
                thinking={'type': 'disabled'},
                output_config=claude.output_config(copy.deepcopy(SCHEMA), effort='low'),
            ), timeout=TIMEOUT)
    response = await call_with_backoff(call, 'anthropic')
    usage.record_claude('brief', response)
    return next((b.text for b in response.content if b.type == 'text'), '')


async def _ask_gemini(user_text):
    async def call():
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        async with client.aio as aio:
            return await asyncio.wait_for(aio.models.generate_content(
                model=settings.GEMINI_API_MODEL[0], contents=user_text,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM, max_output_tokens=900, response_mime_type='application/json',
                    response_schema=copy.deepcopy(SCHEMA), thinking_config=types.ThinkingConfig(thinking_budget=0)),
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


MIN_PROMPT_CHARS = 20
RETRY_HINT = ('\nYour previous brief was unusable (empty, too short, or it relied on text, charts or labels). '
              'Return a concrete picture of people and objects only, with no text of any kind.')
GENERIC_SUBJECTS = {'object', 'thing', 'item', 'something', 'concept', 'idea', 'symbol', 'scene', 'abstract concept'}


def _usable(kind, subject, prompt):
    return kind in ('object', 'action', 'scene') and bool(subject) and len(prompt) >= MIN_PROMPT_CHARS and not undrawable(subject, prompt)


def fallback_brief(word, pos, definition):
    """Brief dự phòng khi AI hỏi lại vẫn hỏng: cảnh đồ vật đơn giản dựng từ chính từ và định nghĩa."""
    gloss = (definition or '').strip().rstrip('.')[:140]
    if undrawable('', gloss):  # định nghĩa nhắc tới biểu đồ / chữ thì bỏ phần giải nghĩa, chỉ giữ từ
        gloss = ''
    meaning = f'the meaning of "{word.strip()}"' + (f' ({gloss})' if gloss else '')
    return {'kind': 'scene', 'subject': f'{word.strip().lower()} {(pos or "meaning").strip().lower()}'[:60],
            'prompt': f'A very simple scene that shows {meaning}: one or two simple everyday objects.'}


async def make_brief(word, pos, definition, example='', ask=None):
    """
    {kind, subject, prompt} cho 1 nghĩa, luôn dùng được (không có none): AI trả hỏng thì hỏi lại 1 lần, vẫn hỏng thì brief dự phòng.
    """
    user = f'Word: {word} ({pos or "?"})\nSense: {definition}' + (f'\nUsage example: {example}' if example else '')
    for attempt in range(2):
        try:
            data = json.loads(await (ask or _ask)(user + (RETRY_HINT if attempt else '')))
        except (ValueError, TypeError):
            continue
        kind = data.get('image_kind')
        subject = (data.get('image_subject') or '').strip().lower()
        prompt = (data.get('image_prompt') or '').strip()
        if subject in GENERIC_SUBJECTS:
            subject = f'{word.strip().lower()} {(pos or "").strip().lower()}'.strip()
        if _usable(kind, subject, prompt):
            return {'kind': kind, 'subject': subject, 'prompt': prompt[:400]}
    return fallback_brief(word, pos, definition)


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


async def draft_briefs(word_value, language_code, sense_objs, ask=None):
    """
    Brief cho các nghĩa NHÁP, chạy song song với bước refine (refine mất ~7s, brief ~1s). Trả
    {sense_id: {'sig': (định nghĩa, loại từ), 'brief': brief}}. Lỗi từng nghĩa chỉ ghi log (nghĩa đó sẽ được viết sau khi lưu).
    """
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
    return sense.metadata_id is not None and (meta.get('source') != provider() or meta.get('kind') not in ('object', 'action', 'scene'))


async def ensure_briefs(senses, ask=None):
    """Viết brief còn thiếu cho các nghĩa (lỗi từng nghĩa chỉ ghi log, nghĩa đó bị bỏ qua lần này). Trả số nghĩa đã viết."""
    todo = [s for s in senses if needs_brief(s) and sense_definition(s)]
    if not todo:
        return 0
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
