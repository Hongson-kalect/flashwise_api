"""
Luồng seed hàng loạt, không socket, chạy theo từng giai đoạn và lưu trạng thái ra thư mục (chạy lại / tiếp tục được):

  prepare  tạo bản ghi AIWord PROCESSING cho từ trong step file
  draft    Claude (batch hoặc online): nghĩa nháp, từ vô nghĩa bị REJECTED
  refine   Claude (batch hoặc online): kiểm tra / sửa / xoá nghĩa, dùng lại apply_refinement của luồng thường
  enrich   Gemini (hàm dịch + metadata của luồng thường): bản dịch các base_languages + metadata (batch: Gemini Batch API, utils/ai/gemini_batch.py)
  save     ghi AISense + metadata, từ COMPLETED
  verify   Claude Haiku (batch hoặc online) kiểm chéo bản dịch của Gemini: ổn -> ai_verify, có bản sửa -> áp bản mới và ai_verify = False (ai/seedflow/verify.py)
  images   brief ảnh (batch: Gemini Batch API, chỗ hỏng viết lại online) + xếp việc vẽ ComfyUI (ai_worker vẽ khi ComfyUI chạy)
  concept  nối từng nghĩa vào concept (embedding + judge), luôn online vì các nghĩa sau cần concept do nghĩa trước vừa tạo

HAI LUỒNG (chọn lúc `start --mode`):
  online  luồng thường: mọi giai đoạn gọi AI ngay, xong trong vài phút. Dùng để thử / kiểm tra chất lượng trước.
  batch   luồng batch: draft, refine, verify (Claude Message Batches) và enrich, brief ảnh (Gemini Batch API) đều gửi batch, giảm 50% giá,
          mỗi giai đoạn chờ vài phút đến vài giờ (chạy lại `run` để đi tiếp). Chỉ concept chạy online. Dùng để seed số lượng lớn.

Trạng thái từng từ: new -> drafted -> refined -> enriched -> saved, hoặc rejected / failed (kèm lý do).
"""
import asyncio
import contextlib
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
from ai.seedflow import external, llm
from ai.seedflow.steps import read_step
from utils.ai import claude, gemini_batch
from utils.ai.prompt import WORD_SYSTEM_PROMPT, render_word_prompt
from utils.ai.refine import REFINE_SYSTEM_PROMPT, apply_refinement, refine_schema, render_refine_prompt, stamp_senses
from utils.ai.word_render import build_sense_obj, common_ipas, get_schema, save_sense

logger = logging.getLogger(__name__)

STAGES = ['prepare', 'draft', 'refine', 'enrich', 'save', 'verify', 'images', 'concept']
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

    @classmethod
    def create_backfill(cls, name, words, language_code, base_languages, mode):
        """
        Run bù cho từ ĐÃ có trong DB (vd từ tạo bằng luồng online cũ thiếu bản dịch / verify / concept): bỏ qua prepare / draft / refine,
        nạp nghĩa hiện có vào `senses` rồi chạy enrich (dịch các ngôn ngữ còn thiếu, giữ metadata có sẵn), save kiểu ghi bù (chỉ cập nhật
        `contents`, không đụng cột khác), verify, images, concept như luồng thường.
        """
        run = cls(name)
        if run.state:
            raise ValueError(f'Run {name} đã có')
        found = {w.value: w for w in AIWord.objects.filter(language_code=language_code, value__in=list(words), status='COMPLETED')}
        missing = [w for w in words if w not in found]
        if missing:
            raise ValueError(f'Không phải từ COMPLETED trong DB: {", ".join(missing)}')
        run.dir.mkdir(parents=True, exist_ok=True)
        run.state = {
            'name': name, 'kind': 'backfill', 'step_file': '', 'language_code': language_code, 'base_languages': base_languages, 'mode': mode,
            'created_at': timezone.now().isoformat(), 'batches': {}, 'usage': {}, 'issues': [], 'log': [],
            'stages': {'prepare': 'done', 'draft': 'done', 'refine': 'done'},
            'words': {v: {'id': str(w.id), 'status': 'refined', 'reason': 'bù', 'reference': [], 'rank': None} for v, w in found.items()},
        }
        for value, w in found.items():
            run.senses[value] = backfill_senses(w)
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
    fixed = {'zero_width': 0, 'foreign_translation': 0, 'whitespace': 0, 'duplicate_items': 0}

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
        # bỏ mục trùng và cắt tối đa 5 mục ở mọi danh sách: bản dịch, tags, image_keywords, synonyms...
        for lang in list(translations):
            fixed['duplicate_items'] += _dedupe_in_place(translations, lang)
        metadata = sense.get('metadata') or {}
        for key in ('tags', 'image_keywords'):
            fixed['duplicate_items'] += _dedupe_in_place(metadata, key)
        for key in list((metadata.get('advanced') or {})):
            fixed['duplicate_items'] += _dedupe_in_place(metadata['advanced'], key)
    return fixed


def _dedupe_in_place(container, key):
    """container[key] là danh sách thì lọc trùng + cắt 5 mục; trả số mục đã bỏ."""
    from utils.ai.lists import dedupe
    before = container.get(key)
    if not isinstance(before, list):
        return 0
    container[key] = dedupe(before)
    return len(before) - len(container[key])


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


async def _enrich_word(run, word, semaphore, record=False):
    """record=True (lượt ghi của luồng batch): chạy một lượt để Recorder gom request, không đổi nghĩa, không dọn, không báo thiếu."""
    from utils.ai.metadata import ai_create_metadata
    from utils.ai.translate import ai_create_translate_sema
    senses = run.senses[word]
    base = run.state['base_languages']
    async with semaphore:
        for attempt in range(1 if record else 2):  # lần 2 chỉ làm lại phần còn thiếu (ở luồng batch: chỗ batch không có kết quả thì gọi online)
            todo = senses if attempt == 0 else [s for s in senses if missing_enrichment([s], run.lang, base)]
            if not todo:
                break
            jobs = [ai_create_translate_sema({'word_value': word, 'language_code': run.lang, 'user_language_code': base, 'missing_translate': todo,
                                              'current_senses': todo, 'base_languages': base})]
            need_meta = [s for s in todo if not s.get('metadata')]
            if need_meta:
                jobs.append(ai_create_metadata({'word_value': word, 'language_code': run.lang, 'current_senses': need_meta}))
            await asyncio.gather(*jobs, return_exceptions=True)
    if record:
        return word, []
    fixed = sanitize_senses(senses, run.lang)
    run.state.setdefault('sanitized', {})[word] = {k: v for k, v in fixed.items() if v}
    return word, missing_enrichment(senses, run.lang, base)


def _run_enrich(run, words, record=False):
    async def go():
        semaphore = asyncio.Semaphore(int(getattr(settings, 'SEED_ENRICH_CONCURRENCY', 4)))
        return await asyncio.gather(*[_enrich_word(run, w, semaphore, record=record) for w in words])

    return asyncio.run(go())


def enrich_provider():
    """Ai viết bản dịch / metadata / brief ảnh ở luồng batch: 'claude' (Haiku) hoặc 'gemini' (SEED_ENRICH_PROVIDER)."""
    return 'claude' if str(getattr(settings, 'SEED_ENRICH_PROVIDER', 'gemini')).strip().lower() == 'claude' else 'gemini'


def metadata_array_schema(schema):
    """
    Schema metadata {id nghĩa: {danh sách...}} -> {'senses': [{'id', danh sách...}]}. Claude biên dịch schema thành grammar và từ chối khi có
    nhiều thuộc tính tuỳ chọn (r273: từ 9 nghĩa x 5 danh sách = 45; bắt buộc hết thì grammar vẫn quá lớn); dạng mảng có kích thước cố định.
    """
    first = next(iter(schema['properties'].values()))
    item = {'type': 'OBJECT', 'properties': {'id': {'type': 'STRING'}, **first['properties']}, 'required': ['id'] + list(first.get('required', []))}
    return {'type': 'OBJECT', 'properties': {'senses': {'type': 'ARRAY', 'items': item}}, 'required': ['senses']}


def metadata_array_to_dict(text):
    """Kết quả dạng mảng -> {id nghĩa: {...}} (dạng mà ai_create_metadata đọc); None khi hỏng."""
    try:
        data = json.loads(text)
        return json.dumps({s['id']: {k: v for k, v in s.items() if k != 'id'} for s in data['senses']}, ensure_ascii=False)
    except (ValueError, KeyError, TypeError):
        return None


def _claude_requests(recorded):
    """Request đã ghi ở dạng Gemini (nội dung + schema) -> request Claude Message Batches; custom_id = khoá băm để Replayer tra lại."""
    out = []
    for key, entry in recorded.items():
        params = {'model': settings.AI_REFINE_MODELS[0], 'max_tokens': entry['max_tokens'],
                  'messages': [{'role': 'user', 'content': entry['contents']}], 'thinking': {'type': 'disabled'},
                  'output_config': claude.output_config(metadata_array_schema(entry['schema']) if entry['label'] == 'metadata' else entry['schema'])}
        if entry.get('system'):
            params['system'] = entry['system']
        out.append({'custom_id': key, 'params': params})
    return out


def _claude_results(run, info, outcomes):
    """Kết quả Claude batch -> dạng Replayer {khoá: {'text','in','out','provider'}}; ghi token + chi phí từng loại (translate / metadata / brief)."""
    results, by_label = {}, {}
    for key, outcome in outcomes.items():
        by_label.setdefault(info['labels'].get(key, 'other'), {})[key] = outcome
        text = outcome.get('text') if outcome.get('ok') and outcome.get('stop_reason') != 'max_tokens' else None
        if text and info['labels'].get(key) == 'metadata':
            text = metadata_array_to_dict(text)
        if text:
            results[key] = {'text': text, 'in': outcome['tokens_in'], 'out': outcome['tokens_out'], 'provider': 'claude'}
        else:
            run.log(f"Claude batch: request {key} không dùng được ({outcome.get('stop_reason') or outcome.get('error')}): chạy online bù")
    for label, group in by_label.items():
        run.add_usage(f'enrich_{label}', group, batch=True)
    return results


def _gemini_phase(run, stage, record):
    """
    Giai đoạn dùng Gemini ở luồng batch. Trả Replayer (kết quả batch đã về, chạy lại hàm của giai đoạn với nó) hoặc None (đang chờ batch).
    record(recorder): gom request của giai đoạn vào recorder (hàm dịch / metadata gom qua hook, brief ảnh gọi recorder.handle trực tiếp).
    Kết quả lưu ở seed_runs/<run>/gemini_<stage>.json nên tiến trình chết giữa chừng vẫn chạy tiếp được, không gửi batch lần hai.
    """
    results_path = run.dir / f'gemini_{stage}.json'
    if results_path.exists():
        return gemini_batch.Replayer(json.loads(results_path.read_text(encoding='utf-8')))
    batches = run.state.setdefault('gbatches', {})
    info = batches.get(stage)
    if info is None:
        recorder = gemini_batch.Recorder()
        with gemini_batch.use(recorder):
            record(recorder)
        if not recorder.requests:
            results_path.write_text('{}', encoding='utf-8', newline='\n')
            return gemini_batch.Replayer({})
        if enrich_provider() == 'claude':
            batch_id = llm.submit_batch(_claude_requests(recorder.requests))
            batches[stage] = {'provider': 'claude', 'batch_id': batch_id, 'requests': len(recorder.requests),
                              'labels': {k: e['label'] for k, e in recorder.requests.items()}}
            run.log(f'{stage}: đã gửi Claude batch {batch_id} ({len(recorder.requests)} request)')
            return None
        jobs = gemini_batch.submit(recorder.requests, f'{run.name}_{stage}')
        batches[stage] = {'jobs': jobs, 'requests': len(recorder.requests)}
        run.log(f'{stage}: đã gửi Gemini batch {len(jobs)} job ({len(recorder.requests)} request)')
        return None
    if info.get('provider') == 'claude':
        status, counts = llm.batch_status(info['batch_id'])
        if status != 'ended':
            run.state['batch_progress'] = {stage: counts}
            run.save()
            return None
        results = _claude_results(run, info, llm.batch_results(info['batch_id']))
        results_path.write_text(json.dumps(results, ensure_ascii=False), encoding='utf-8', newline='\n')
        run.log(f'{stage}: Claude batch xong ({len(results)}/{info["requests"]} request có kết quả; {counts})')
        return gemini_batch.Replayer(results)
    results, running, dead = {}, 0, 0
    for job in info['jobs']:
        state, got = gemini_batch.poll(job)
        running += state == 'running'
        dead += state == 'dead'
        results.update(got)
    if running:
        run.state['batch_progress'] = {stage: {'gemini_jobs': len(info['jobs']), 'đang chạy': running}}
        run.save()
        return None
    if dead:
        run.state['issues'].append({'word': '*', 'stage': stage, 'missing': [f'{dead} Gemini batch job hỏng / hết hạn: chỗ đó chạy online']})
    results_path.write_text(json.dumps(results, ensure_ascii=False), encoding='utf-8', newline='\n')
    run.log(f'{stage}: Gemini batch xong ({len(results)}/{info["requests"]} request có kết quả)')
    return gemini_batch.Replayer(results)


def stage_enrich(run):
    words = run.words_in('refined')
    replayer = None
    if run.state['mode'] == 'batch' and words:
        replayer = _gemini_phase(run, 'enrich', lambda recorder: _run_enrich(run, words, record=True))
        if replayer is None:
            return False
    with (gemini_batch.use(replayer) if replayer else contextlib.nullcontext()):
        outcomes = _run_enrich(run, words)
    if replayer:
        run.state['enrich_batch'] = {'từ batch': replayer.hits, 'gọi online bù': replayer.misses}
    for word, missing in outcomes:
        if missing:
            run.state['issues'].append({'word': word, 'stage': 'enrich', 'missing': missing})
        run.mark(word, 'enriched', '; '.join(missing))
    return True


# ---------------------------------------------------------------- save / images / concept

def backfill_senses(word):
    """Nghĩa đang dùng của từ đã lưu, dạng dict như luồng seed (id, pos, contents...). Metadata có sẵn thì chỉ giữ id để enrich không sinh lại."""
    out = []
    for s in AISense.objects.filter(word=word, is_deleted=False, is_official=True).order_by('created_at', 'id'):
        out.append({'id': str(s.id), 'word_id': str(word.id), 'word_value': word.value, 'language_code': s.language_code, 'pos': s.pos,
                    'level': s.level, 'register': s.register, 'ipas': s.ipas, 'contents': copy.deepcopy(s.contents or {}),
                    'metadata': {'id': str(s.metadata_id)} if s.metadata_id else None})
    return out


def _definition_text(contents, lang):
    node = (contents.get('definition') or {}).get(lang)
    return json.dumps(node, sort_keys=True, ensure_ascii=False)


def save_backfill(word_id, senses):
    """Ghi bù: chỉ cập nhật `contents` (+ tạo metadata nếu nghĩa chưa có), giữ mọi cột khác. Ngôn ngữ nào bản dịch đổi thì bỏ verify của ngôn ngữ đó."""
    from ai.models import AISenseMetadata
    from utils.image_gen.service import _recache_word
    from utils.utils.best_effort import best_effort
    for s in senses:
        inst = AISense.objects.get(id=s['id'])
        new = s['contents']
        for lang in list((inst.verify_state or {}).keys()):
            if _definition_text(inst.contents or {}, lang) != _definition_text(new, lang):
                inst.verify_state.pop(lang, None)
        inst.contents = new
        meta = s.get('metadata') or {}
        if not inst.metadata_id and len(meta) > 1:
            AISenseMetadata(**meta).save()
            inst.metadata_id = meta['id']
        inst.save(update_fields=['contents', 'verify_state', 'metadata', 'updated_at'])
    best_effort(_recache_word, word_id)


def stage_save(run):
    if run.state.get('kind') == 'backfill':
        for word in run.words_in('enriched'):
            save_backfill(run.state['words'][word]['id'], run.senses[word])
            run.mark(word, 'saved', run.state['words'][word]['reason'])
        return True
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


def stage_verify(run):
    """Kiểm chéo bản dịch: mỗi từ đã lưu một lệnh Haiku (batch giảm 50% giá). Tắt bằng SEED_VERIFY_ENABLED=false."""
    if not getattr(settings, 'SEED_VERIFY_ENABLED', True):
        run.log('verify: SEED_VERIFY_ENABLED tắt, bỏ qua')
        return True
    from ai.seedflow import verify_db
    langs = run.state['base_languages']
    requests, meta = verify_db.build_requests([run.state['words'][w]['id'] for w in run.words_in('saved')], run.lang, langs)
    if not requests:
        return True
    agent_file = run.dir / 'verify_external.json'
    if agent_file.exists():  # agent ngoài đã kiểm (external.export_verify / import_verify): không gọi API
        results, model = json.loads(agent_file.read_text(encoding='utf-8')), external.MODEL_TAG
    elif run.state.get('agent_verify'):
        return False  # chờ agent: chạy export-verify, agent viết file ra, import-verify rồi chạy lại
    else:
        results, done = _batch_or_online(run, 'verify', requests)
        model = None
        if not done:
            return False
    run.state['verify_counts'] = verify_db.apply_results(meta, results, run.lang, model=model)
    run.log(f"verify: {run.state['verify_counts']}")
    return True


def _brief_chunks(run):
    """Các lệnh gộp brief ảnh còn thiếu của từ đã lưu: [(từ, nhóm nghĩa [{'key','pos','definition','example'}], nội dung hỏi)] + {id nghĩa: AISense}."""
    from utils.image_gen import brief
    ids = [run.state['words'][w]['id'] for w in run.words_in('saved')]
    senses = list(AISense.objects.filter(word_id__in=ids).select_related('metadata'))
    by_word = {}
    for sense in senses:
        if brief.needs_brief(sense) and brief.sense_definition(sense):
            by_word.setdefault(sense.word_id, []).append(sense)
    chunks = []
    for group in by_word.values():
        items = [{'key': str(x.id), 'pos': x.pos, 'definition': brief.sense_definition(x), 'example': brief.sense_example(x)} for x in group]
        chunks += [(group[0].word_value, chunk, question) for chunk, question in brief.brief_chunks(group[0].word_value, items)]
    return chunks, {str(s.id): s for s in senses}


def _prefill_briefs(run):
    """
    Luồng batch: viết brief ảnh bằng Gemini Batch (cùng prompt và schema với lệnh gộp online). Trả False khi đang chờ batch.
    Nghĩa batch không viết được / viết hỏng thì để prepare_and_enqueue viết lại online như bình thường.
    """
    from utils.image_gen import brief
    provider = enrich_provider()
    if brief.provider() != provider:
        run.log(f'images: IMAGE_BRIEF_PROVIDER ({brief.provider()}) khác SEED_ENRICH_PROVIDER ({provider}), brief chạy online')
        return True
    chunks, senses = _brief_chunks(run)
    model = settings.AI_REFINE_MODELS[0] if provider == 'claude' else settings.GEMINI_API_MODEL[0]
    # Gemini Batch (r232) không làm theo systemInstruction (trả image_subject dài, thiếu image_prompt, 4/10 lệnh lặp tới trần): nhét luật vào nội dung lệnh
    ask = (model, brief.BATCH_SCHEMA, None, 2500)
    prefix = brief.BATCH_SYSTEM + '\n\n'

    def record(recorder):
        for _, _, question in chunks:
            recorder.handle('brief', model, prefix + question, ask[1], ask[2], ask[3])

    replayer = _gemini_phase(run, 'briefs', record)
    if replayer is None:
        return False
    written = 0
    for word, chunk, question in chunks:
        data = replayer.handle('brief', model, prefix + question, ask[1], ask[2], ask[3])
        if data is None:
            continue
        made, _ = brief.parse_chunk_briefs(word, chunk, data)
        for key, result in made.items():
            meta = senses[key].metadata
            meta.image_metadata = {**(meta.image_metadata or {}), 'subject': result['subject'], 'prompt': result['prompt'],
                                   'kind': result['kind'], 'source': brief.provider()}
            meta.save(update_fields=['image_metadata'])
            written += 1
    run.state['briefs_batch'] = {'brief batch viết': written, 'lệnh gộp': len(chunks), 'lệnh phải gọi online bù': replayer.misses}
    return True


def stage_images(run):
    if not settings.IMAGE_GEN_ENABLED:
        run.log('images: IMAGE_GEN_ENABLED tắt, bỏ qua')
        return True
    if run.state['mode'] == 'batch' and not _prefill_briefs(run):
        return False
    from utils.image_gen.service import prepare_and_enqueue

    async def go():
        for word in run.words_in('saved'):
            await prepare_and_enqueue(run.state['words'][word]['id'])
    asyncio.run(go())
    return True


def _queue_concepts(run):
    """Đẩy nghĩa chưa có concept của từ đã lưu vào hàng Redis theo ngôn ngữ nghĩa (thứ tự tạo); consumer nối sau. Chạy lại không đẩy trùng quá mức: link_sense bỏ qua nghĩa đã nối."""
    from utils.concept import queue
    ids = [run.state['words'][w]['id'] for w in run.words_in('saved')]
    by_lang = {}
    for sense_id, lang in (AISense.objects.filter(word_id__in=ids, is_official=True, concept__isnull=True)
                           .order_by('created_at', 'id').values_list('id', 'language_code')):
        by_lang.setdefault(lang or '', []).append(sense_id)
    run.state['concept_queued'] = {lang: queue.push(lang, group) for lang, group in by_lang.items()}
    run.log(f"concept: đã xếp hàng Redis {run.state['concept_queued']} (chạy `seed_flow concept-worker --lang <ngôn ngữ>` để nối)")
    return True


def stage_concept(run):
    from utils.concept.judge import judge as api_judge
    from utils.concept.linker import link_word
    kwargs, counters = {}, None
    agent_file = run.dir / 'concept_external.json'
    if agent_file.exists():  # agent ngoài đã judge (external.export_concept / import_concept); chỗ agent không quyết thì judge cấu hình sẵn bù
        judge_fn, counters = external.concept_judge(json.loads(agent_file.read_text(encoding='utf-8')), api_judge)
        kwargs = {'judge_fn': judge_fn, 'model_label': external.MODEL_TAG}
    elif run.state.get('agent_concept'):
        return False  # chờ agent: export-concept, agent viết file ra, import-concept rồi chạy lại
    elif getattr(settings, 'SEED_CONCEPT_QUEUE', False):
        return _queue_concepts(run)
    outcomes = {}
    for word in run.words_in('saved'):
        for r in link_word(run.state['words'][word]['id'], **kwargs):
            outcomes[r['outcome']] = outcomes.get(r['outcome'], 0) + 1
    run.state['concept_outcomes'] = outcomes
    if counters:
        run.state['concept_judge'] = counters
    return True


STAGE_FUNCS = {'prepare': stage_prepare, 'draft': stage_draft, 'refine': stage_refine, 'enrich': stage_enrich,
               'save': stage_save, 'verify': stage_verify, 'images': stage_images, 'concept': stage_concept}


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


# ---------------------------------------------------------------- pipeline lệch pha giữa các step

def pipeline(next_run, max_active=2, admit_after='draft', poll_seconds=60, sleep=time.sleep, say=print, max_errors=3):
    """
    Chạy nhiều step lệch pha: step N đang chờ batch refine thì step N+1 đã gửi draft. Mỗi vòng quét gọi advance(wait=False) cho từng run đang chạy
    (một tiến trình, tuần tự; mỗi run có .lock riêng nên không giẫm nhau), rồi ngủ poll_seconds. 30-60s để dưới hạn 50 request/phút của batch ở tier Evaluation.
    next_run(): trả Run kế tiếp (tạo / mở) hoặc None khi hết step.
    Chỉ nhận run mới khi còn chỗ (< max_active) VÀ mọi run đang chạy đã qua stage admit_after: các step cách nhau đúng một stage.
    Run lỗi liên tiếp max_errors lần thì bị bỏ khỏi vòng (vẫn chạy tiếp được bằng `seed_flow run`). Trả {'done': [tên], 'dropped': [tên]}.
    """
    active, done, dropped, errors, exhausted = [], [], [], {}, False
    while True:
        while not exhausted and len(active) < max_active and all(r.state['stages'].get(admit_after) == 'done' for r in active):
            run = next_run()
            if run is None:
                exhausted = True
                break
            say(f'pipeline: nhận {run.name} ({len(run.state["words"])} từ)')
            active.append(run)
        if not active:
            return {'done': done, 'dropped': dropped}
        for run in list(active):
            try:
                waiting = advance(run, wait=False)
                errors[run.name] = 0
            except Exception as e:
                errors[run.name] = errors.get(run.name, 0) + 1
                say(f'pipeline: {run.name} lỗi ({errors[run.name]}/{max_errors}): {e}')
                if errors[run.name] >= max_errors:
                    active.remove(run)
                    dropped.append(run.name)
                continue
            if waiting is None:
                active.remove(run)
                done.append(run.name)
                say(f'pipeline: {run.name} xong')
            else:
                say(f'pipeline: {run.name} chờ ở {waiting}')
        if active or not exhausted:
            sleep(poll_seconds)
