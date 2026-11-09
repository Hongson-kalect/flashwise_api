"""Gemini xác nhận một nghĩa có THỰC SỰ cùng nghĩa với concept ứng viên nào không (phép thử dịch), chạy nhiều lần với thứ tự ứng viên đảo."""
import copy
import json
import logging
import time

from django.conf import settings
from google import genai
from google.genai import types

from utils.ai import usage

logger = logging.getLogger(__name__)

SYSTEM = (
    "You link word senses from any language to a shared list of language-neutral concepts. Given ONE sense (its English gloss and English definition) and up to 6 "
    "candidate concepts, pick a candidate ONLY if a translator would use the SAME word for both in another language, i.e. the sense and the candidate mean the very same "
    "thing and one could replace the other in nearly every context. Differences that do NOT matter (still SAME): formality or register (hi / hello), spelling, region, "
    "inflection, a different word for exactly the same meaning. Differences that matter (answer 0, NEW): degree or intensity (big / huge / vast), dimension (wide / long), "
    "a part, a use, broader or narrower meaning, a different object that merely shares a word, a different part of speech with a different meaning, a different sense "
    "of a polysemous word. Examples of NEW: bench vs sofa; a machine part vs a drum container; a dental bridge vs a bridge over a river; a holiday vs a long weekend joined "
    "to it; wide vs vast; tiny vs small. Examples of SAME: a hard head protector vs a safety helmet; hi vs hello; buy vs purchase. "
    "Also list in `near` the candidate numbers that are closely related but NOT the same (near-synonyms, broader or narrower), useful for a later synonym layer. "
    "Answer `choice` = candidate number (1-6) or 0 for none."
)
SCHEMA = {
    "type": "OBJECT",
    "properties": {"choice": {"type": "INTEGER"}, "near": {"type": "ARRAY", "items": {"type": "INTEGER"}}, "why": {"type": "STRING"}},
    "required": ["choice", "near", "why"],
}
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def model_name():
    return getattr(settings, 'CONCEPT_JUDGE_MODEL', '') or settings.GEMINI_API_MODEL[0]


def _ask(prompt):
    last = None
    for attempt in range(4):
        try:
            model = model_name()
            extra = {'thinking_config': types.ThinkingConfig(thinking_budget=0)} if model.startswith('gemini-2.5') else {}  # 3.x trả 400 với thinking_budget=0
            resp = _get_client().models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(system_instruction=SYSTEM, max_output_tokens=300, temperature=0, response_mime_type='application/json',
                                                   response_schema=copy.deepcopy(SCHEMA), **extra))
            usage.record_gemini('concept_judge', resp)
            return json.loads(resp.text)
        except Exception as e:
            last = e
            logger.warning('Judge concept lỗi (lần %s): %s', attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    raise last


def _lines(cands):
    return '\n'.join(f"{i + 1}. {c['label']} [{c['pos']}]: {c['description']}" for i, c in enumerate(cands))


def judge(sense_info, candidates, runs=None):
    """
    sense_info: {head, pos, definition}; candidates: [{id, label, pos, description}].
    Trả {'concept_id': id|None (chỉ khi mọi lần chọn cùng một concept), 'near': [id], 'runs': [...], 'agree': bool}.
    """
    runs = runs or settings.CONCEPT_JUDGE_RUNS
    if not candidates:
        return {'concept_id': None, 'near': [], 'runs': [], 'agree': True}
    results = []
    for k in range(runs):
        order = list(range(len(candidates))) if k % 2 == 0 else list(reversed(range(len(candidates))))  # lần lẻ đảo thứ tự
        shown = [candidates[i] for i in order]
        prompt = f"Sense: gloss \"{sense_info['head']}\" [{sense_info['pos']}]: {sense_info['definition']}\nCandidates:\n{_lines(shown)}"
        data = _ask(prompt)
        ch = data.get('choice') or 0
        chosen = shown[ch - 1]['id'] if 1 <= ch <= len(shown) else None
        near = [shown[n - 1]['id'] for n in (data.get('near') or []) if 1 <= n <= len(shown) and n != ch]
        results.append({'chosen': chosen, 'near': near, 'why': (data.get('why') or '')[:200]})
    chosen = {r['chosen'] for r in results}
    agree = len(chosen) == 1
    near = []
    for r in results:
        for n in r['near']:
            if n not in near:
                near.append(n)
    # ứng viên mà một lần chọn nhưng lần khác không đồng ý cũng là "gần nhưng chưa chắc"
    for c in chosen:
        if c and not agree and c not in near:
            near.append(c)
    return {'concept_id': next(iter(chosen)) if agree else None, 'near': near, 'runs': results, 'agree': agree}
