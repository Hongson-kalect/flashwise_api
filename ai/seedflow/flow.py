"""
Luồng seed hàng loạt, không socket, chạy theo từng giai đoạn và lưu trạng thái ra thư mục (chạy lại / tiếp tục được):

  prepare  tạo bản ghi AIWord PROCESSING cho từ trong step file
  draft    Claude (batch hoặc online): nghĩa nháp, từ vô nghĩa bị REJECTED
  refine   Claude (batch hoặc online): kiểm tra / sửa / xoá nghĩa, dùng lại apply_refinement của luồng thường
  enrich   Gemini online (hàm dịch + metadata của luồng thường): bản dịch các base_languages + metadata
  save     ghi AISense + metadata, từ COMPLETED
  images   brief + xếp việc vẽ ComfyUI (ai_worker vẽ khi ComfyUI chạy)
  concept  nối từng nghĩa vào concept (embedding + judge)

Trạng thái từng từ: new -> drafted -> refined -> enriched -> saved, hoặc rejected / failed (kèm lý do).
"""
import asyncio
import copy
import json
import logging
import re
import time
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from ai.models import AISense, AIWord
from ai.seedflow import llm
from ai.seedflow.steps import read_step
from utils.ai import claude
from utils.ai.prompt import WORD_SYSTEM_PROMPT, render_word_prompt
from utils.ai.refine import REFINE_SYSTEM_PROMPT, apply_refinement, refine_schema, render_refine_prompt, stamp_senses
from utils.ai.word_render import build_sense_obj, common_ipas, get_schema, save_sense

logger = logging.getLogger(__name__)

STAGES = ['prepare', 'draft', 'refine', 'enrich', 'save', 'images', 'concept']
LATIN_LANGS = ['vi', 'en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'pl', 'sv', 'no', 'da', 'fi', 'tr', 'cs', 'hu', 'id']
RUNS_DIR = Path(settings.BASE_DIR) / 'seed_runs'


def schema_mode(language_code):
    return 'latin' if language_code in LATIN_LANGS else ('complex' if language_code == 'ja' else 'flat')


class Run:
    """Một lần chạy seed: thư mục seed_runs/<name>/ gồm state.json (trạng thái) và senses.json (nghĩa đang xử lý)."""

    def __init__(self, name):
        self.name = name
        self.dir = RUNS_DIR / name
        self.state_path = self.dir / 'state.json'
        self.senses_path = self.dir / 'senses.json'
        self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else None
        self.senses = json.loads(self.senses_path.read_text(encoding='utf-8')) if self.senses_path.exists() else {}

    @classmethod
    def create(cls, name, step_file, language_code, base_languages, mode):
        run = cls(name)
        if run.state:
            raise ValueError(f'Run {name} đã có')
        run.dir.mkdir(parents=True, exist_ok=True)
        rows = read_step(step_file)
        run.state = {
            'name': name, 'step_file': str(step_file), 'language_code': language_code, 'base_languages': base_languages, 'mode': mode,
            'created_at': timezone.now().isoformat(), 'stages': {}, 'batches': {}, 'usage': {}, 'issues': [], 'log': [],
            'words': {r['w']: {'id': None, 'status': 'new', 'reason': '', 'reference': r.get('reference') or [], 'rank': r.get('rank')} for r in rows},
        }
        run.save()
        return run

    def save(self):
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding='utf-8', newline='\n')
        self.senses_path.write_text(json.dumps(self.senses, ensure_ascii=False), encoding='utf-8', newline='\n')

    def log(self, message):
        self.state['log'].append(f'{timezone.now().strftime("%H:%M:%S")} {message}')
        self.save()

    def words_in(self, *statuses):
        return [w for w, info in self.state['words'].items() if info['status'] in statuses]

    def mark(self, word, status, reason=''):
        self.state['words'][word]['status'] = status
        self.state['words'][word]['reason'] = reason

    def add_usage(self, stage, results, batch):
        u = self.state['usage'].setdefault(stage, {'in': 0, 'out': 0, 'usd': 0.0, 'requests': 0})
        tin = sum(r['tokens_in'] for r in results.values())
        tout = sum(r['tokens_out'] for r in results.values())
        u['in'] += tin
        u['out'] += tout
        u['requests'] += len(results)
        u['usd'] = round(u['usd'] + llm.cost(tin, tout, batch), 5)

    @property
    def lang(self):
        return self.state['language_code']


# ---------------------------------------------------------------- prepare

def stage_prepare(run):
    user = get_user_model().objects.filter(is_superuser=True).order_by('id').first()
    if user is None:
        raise ValueError('Cần ít nhất một superuser để đứng tên các từ')
    existing = {w.value: w for w in AIWord.objects.filter(language_code=run.lang, value__in=list(run.state['words']))}
    for word, info in run.state['words'].items():
        inst = existing.get(word)
        if inst is None:
            inst = AIWord.objects.create(value=word, language_code=run.lang, created_by=user, status='PROCESSING')
        elif inst.status in ('COMPLETED', 'REJECTED', 'BANNED'):
            run.mark(word, 'skipped', f'đã có ({inst.status})')
            info['id'] = str(inst.id)
            continue
        else:
            AIWord.objects.filter(pk=inst.pk).update(status='PROCESSING', updated_at=timezone.now())
        info['id'] = str(inst.id)
    return True


# ---------------------------------------------------------------- Claude stages (draft / refine)

def _batch_or_online(run, stage, requests):
    """Trả (kết quả {custom_id: outcome}, xong?). Batch: submit lần đầu, các lần sau hỏi trạng thái; xong thì tải kết quả."""
    mode = run.state['mode']
    if mode == 'online':
        results = asyncio.run(llm.run_online(requests, concurrency=int(getattr(settings, 'SEED_ONLINE_CONCURRENCY', 5))))
        run.add_usage(stage, results, batch=False)
        return results, True
    batch_id = run.state['batches'].get(stage)
    if not batch_id:
        batch_id = llm.submit_batch(requests)
        run.state['batches'][stage] = batch_id
        run.log(f'{stage}: đã gửi batch {batch_id} ({len(requests)} request)')
    status, counts = llm.batch_status(batch_id)
    if status != 'ended':
        run.state['batch_progress'] = {stage: counts}
        run.save()
        return None, False
    results = llm.batch_results(batch_id)
    run.add_usage(stage, results, batch=True)
    run.log(f'{stage}: batch xong {counts}')
    return results, True


def _draft_params(run, word, info):
    prompt = render_word_prompt(word, run.lang, info.get('reference') or None)
    return {'model': settings.AI_DRAFT_MODELS[0], 'max_tokens': 8192, 'system': WORD_SYSTEM_PROMPT,
            'messages': [{'role': 'user', 'content': prompt}], 'thinking': {'type': 'disabled'},
            'output_config': claude.output_config(get_schema(schema_mode(run.lang)))}


def stage_draft(run):
    words = run.words_in('new')
    requests = [{'custom_id': run.state['words'][w]['id'], 'params': _draft_params(run, w, run.state['words'][w])} for w in words]
    results, done = _batch_or_online(run, 'draft', requests) if requests else ({}, True)
    if not done:
        return False
    for word in words:
        info = run.state['words'][word]
        outcome = results.get(info['id'])
        data = llm.parse_json(outcome)
        if data is None:
            reason = (outcome or {}).get('stop_reason') or (outcome or {}).get('error') or 'no_result'
            run.mark(word, 'failed', f'draft: {reason}')
            continue
        if not (data.get('metadata') or {}).get('should_be_saved', True):
            run.mark(word, 'rejected', 'draft: should_be_saved=false')
            continue
        senses = [build_sense_obj(s, info['id'], word, run.lang) for s in (data.get('senses') or []) if isinstance(s, dict)]
        senses = [s for s in senses if s.get('pos') and ((s['contents']['definition'].get(run.lang) or {}).get('value') or '').strip()]
        if not senses:
            run.mark(word, 'failed', 'draft: không có nghĩa')
            continue
        run.senses[word] = senses
        run.mark(word, 'drafted')
    return True


def _keep_draft(senses, model):
    for sense in senses:
        sense['quality_status'] = 'draft'
        sense['ai_pass'] = 1
        sense['ai_model'] = model
    return senses


def stage_refine(run):
    words = run.words_in('drafted')
    if not settings.AI_REFINE_ENABLED:
        for w in words:
            run.senses[w] = _keep_draft(run.senses[w], settings.AI_DRAFT_MODELS[0])
            run.mark(w, 'refined', 'refine tắt')
        return True
    requests = []
    for w in words:
        info = run.state['words'][w]
        prompt = render_refine_prompt(w, run.lang, run.senses[w], info.get('reference') or None)
        requests.append({'custom_id': info['id'], 'params': {
            'model': settings.AI_REFINE_MODELS[0], 'max_tokens': 8192, 'system': REFINE_SYSTEM_PROMPT,
            'messages': [{'role': 'user', 'content': prompt}], 'thinking': {'type': 'disabled'},
            'output_config': claude.output_config(refine_schema, effort=settings.AI_REFINE_EFFORT)}})
    results, done = _batch_or_online(run, 'refine', requests) if requests else ({}, True)
    if not done:
        return False
    for w in words:
        info = run.state['words'][w]
        senses = run.senses[w]
        data = llm.parse_json(results.get(info['id']))
        if not data or not isinstance(data.get('senses'), list):
            run.senses[w] = _keep_draft(senses, settings.AI_DRAFT_MODELS[0])
            run.mark(w, 'refined', 'refine lỗi: giữ nguyên draft')
            continue
        pristine = copy.deepcopy(senses)
        try:
            survivors, report = apply_refinement(senses, data, run.lang)
            stamp_senses(survivors, report, settings.AI_REFINE_MODELS[0])
        except Exception as e:  # hỏng giữa chừng thì quay về đúng bản draft
            logger.exception('Áp refine %s lỗi', w)
            run.senses[w] = _keep_draft(pristine, settings.AI_DRAFT_MODELS[0])
            run.mark(w, 'refined', f'áp refine lỗi: {e}')
            continue
        run.senses[w] = survivors
        run.mark(w, 'refined', f'sửa {len(report["changed"])}, xoá {len(report["removed"])}')
        info['refine'] = {'changed': [{'fields': c['fields'], 'reason': c['reason']} for c in report['changed']],
                          'removed': [r.get('reason', '') for r in report['removed']]}
    return True


# ---------------------------------------------------------------- enrich (dịch + metadata, Gemini)

ZERO_WIDTH = re.compile('[​‌‍⁠﻿]')
LATIN_OR_VI = re.compile('[A-Za-zÀ-ỹ]')  # chữ Latin (gồm dấu tiếng Việt): không được có trong bản dịch sang ja / zh / ko
CJK_TARGETS = ('ja', 'zh', 'ko')


def sanitize_senses(senses, language_code):
    """
    Dọn lỗi Gemini rẻ hay mắc ở bản dịch (đo trên 20 từ đầu): ký tự rộng 0 chen trong chữ, mục `translations` sang ja / zh / ko là chữ Latin
    (chép nguyên từ gốc như 'that', hoặc lẫn tiếng Việt ' vì'), khoảng trắng thừa. Sửa tại chỗ; trả số lần sửa theo loại.
    """
    fixed = {'zero_width': 0, 'foreign_translation': 0, 'whitespace': 0}

    def clean_text(text):
        if not isinstance(text, str):
            return text
        out = ZERO_WIDTH.sub('', text)
        fixed['zero_width'] += out != text
        stripped = out.strip()
        fixed['whitespace'] += stripped != out
        return stripped

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                node[key] = clean_text(value) if isinstance(value, str) else walk(value)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                node[i] = clean_text(value) if isinstance(value, str) else walk(value)
        return node

    for sense in senses:
        contents = sense.get('contents') or {}
        walk(contents)
        translations = contents.get('translations') or {}
        for lang in list(translations):
            if lang not in CJK_TARGETS or not isinstance(translations[lang], list):
                continue
            kept = []
            for item in translations[lang]:
                text = item.get('value') if isinstance(item, dict) else item
                if isinstance(text, str) and LATIN_OR_VI.search(text):
                    fixed['foreign_translation'] += 1
                else:
                    kept.append(item)
            translations[lang] = kept
    return fixed


def missing_enrichment(senses, language_code, base_languages):
    """Mô tả phần còn thiếu của các nghĩa sau khi dịch + metadata: ['<pos>: thiếu bản dịch vi', 'thiếu metadata']."""
    wanted = [l for l in base_languages if l != language_code]
    out = []
    for s in senses:
        definition = (s.get('contents') or {}).get('definition') or {}
        absent = [l for l in wanted if not ((definition.get(l) or {}).get('value') if isinstance(definition.get(l), dict) else definition.get(l))]
        if absent:
            out.append(f"{s.get('pos')}: thiếu bản dịch {','.join(absent)}")
        if not s.get('metadata'):
            out.append(f"{s.get('pos')}: thiếu metadata")
    return out


async def _enrich_word(run, word, semaphore):
    from utils.ai.metadata import ai_create_metadata
    from utils.ai.translate import ai_create_translate_sema
    senses = run.senses[word]
    base = run.state['base_languages']
    async with semaphore:
        for attempt in range(2):  # lần 2 chỉ làm lại phần còn thiếu
            todo = senses if attempt == 0 else [s for s in senses if missing_enrichment([s], run.lang, base)]
            if not todo:
                break
            jobs = [ai_create_translate_sema({'word_value': word, 'language_code': run.lang, 'user_language_code': base, 'missing_translate': todo,
                                              'current_senses': todo, 'base_languages': base})]
            need_meta = [s for s in todo if not s.get('metadata')]
            if need_meta:
                jobs.append(ai_create_metadata({'word_value': word, 'language_code': run.lang, 'current_senses': need_meta}))
            await asyncio.gather(*jobs, return_exceptions=True)
    fixed = sanitize_senses(senses, run.lang)
    run.state.setdefault('sanitized', {})[word] = {k: v for k, v in fixed.items() if v}
    return word, missing_enrichment(senses, run.lang, base)


def stage_enrich(run):
    words = run.words_in('refined')

    async def go():
        semaphore = asyncio.Semaphore(int(getattr(settings, 'SEED_ENRICH_CONCURRENCY', 4)))
        return await asyncio.gather(*[_enrich_word(run, w, semaphore) for w in words])

    for word, missing in asyncio.run(go()):
        if missing:
            run.state['issues'].append({'word': word, 'stage': 'enrich', 'missing': missing})
        run.mark(word, 'enriched', '; '.join(missing))
    return True


# ---------------------------------------------------------------- save / images / concept

def stage_save(run):
    for word in run.words_in('enriched'):
        info = run.state['words'][word]
        senses = run.senses[word]
        save_sense(copy.deepcopy(senses), info['id'], [], common_ipas(senses))
        run.mark(word, 'saved', info['reason'])
    for word in run.words_in('rejected'):
        AIWord.objects.filter(id=run.state['words'][word]['id']).update(status='REJECTED', updated_at=timezone.now())
    for word in run.words_in('failed'):
        AIWord.objects.filter(id=run.state['words'][word]['id']).exclude(status='COMPLETED').update(status='FAILED', updated_at=timezone.now())
    return True


def stage_images(run):
    if not settings.IMAGE_GEN_ENABLED:
        run.log('images: IMAGE_GEN_ENABLED tắt, bỏ qua')
        return True
    from utils.image_gen.service import prepare_and_enqueue

    async def go():
        for word in run.words_in('saved'):
            await prepare_and_enqueue(run.state['words'][word]['id'])
    asyncio.run(go())
    return True


def stage_concept(run):
    from utils.concept.linker import link_word
    outcomes = {}
    for word in run.words_in('saved'):
        for r in link_word(run.state['words'][word]['id']):
            outcomes[r['outcome']] = outcomes.get(r['outcome'], 0) + 1
    run.state['concept_outcomes'] = outcomes
    return True


STAGE_FUNCS = {'prepare': stage_prepare, 'draft': stage_draft, 'refine': stage_refine, 'enrich': stage_enrich,
               'save': stage_save, 'images': stage_images, 'concept': stage_concept}


LOCK_STALE_SECONDS = 300


def _acquire_lock(run):
    lock = run.dir / '.lock'
    if lock.exists() and time.time() - lock.stat().st_mtime < LOCK_STALE_SECONDS:
        raise RuntimeError(f'Run {run.name} đang được tiến trình khác chạy (khoá {lock}); chờ {LOCK_STALE_SECONDS}s nếu tiến trình đó đã chết')
    lock.write_text(str(time.time()))
    return lock


def advance(run, until=None, wait=True, poll_seconds=30, sleep=time.sleep):
    """Chạy các giai đoạn còn lại theo thứ tự. wait=False: gặp batch chưa xong thì dừng (chạy lại lệnh sau để tiếp). Trả giai đoạn đang chờ hoặc None."""
    lock = _acquire_lock(run)
    try:
        return _advance(run, lock, until, wait, poll_seconds, sleep)
    finally:
        lock.unlink(missing_ok=True)


def _advance(run, lock, until, wait, poll_seconds, sleep):
    for stage in STAGES:
        if run.state['stages'].get(stage) == 'done':
            continue
        while True:
            started = time.monotonic()
            finished = STAGE_FUNCS[stage](run)
            run.save()
            if finished:
                run.state['stages'][stage] = 'done'
                run.log(f'{stage}: xong ({time.monotonic() - started:.0f}s)')
                break
            if not wait:
                return stage
            lock.touch()
            sleep(poll_seconds)
        if until and stage == until:
            return None
    return None
