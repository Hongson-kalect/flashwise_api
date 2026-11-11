"""
Dữ liệu từ do người / agent ngoài luồng "render toàn bộ" (ví dụ subagent Claude Haiku trong phiên Claude Code) thay cho draft + refine + dịch + metadata + brief ảnh:

  export  <run> --dir D   ghi D/INSTRUCTIONS.md (quy tắc viết, lấy từ prompt của luồng thường) và D/<từ>.in.json cho từng từ 'new'
  (agent đọc INSTRUCTIONS.md, mỗi từ viết D/<từ>.out.json theo schema trong đó; xong từ nào ghi từ đó)
  import  <run> --dir D   đọc D/<từ>.out.json, kiểm schema + brief, dựng nghĩa y hệt kết quả của draft → refine → enrich và đặt từ ở trạng thái 'enriched'

Sau import, `run` đi tiếp từ save: verify (Claude, độc lập với người viết), images (brief đã có nên không gọi lại), concept.
Từ không có file ra / sai schema giữ trạng thái 'new' nên các giai đoạn draft / refine / enrich của luồng thường vẫn làm nốt cho chúng.
"""
import json
import re
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from utils.ai.constain import language_map
from utils.ai.prompt import render_word_prompt
from utils.ai.word_render import build_sense_obj
from utils.utils import uuidv7

SAFE = re.compile(r'[^A-Za-z0-9_-]+')
MODEL_TAG = 'claude-haiku-5-5 (subagent)'
METADATA_LISTS = ('collocations', 'idioms', 'relateds', 'synonyms', 'antonyms')


def file_stem(word):
    return SAFE.sub('_', word) or 'word'


def validate(answer, schema, path='$'):
    """Lỗi so với schema kiểu Gemini (OBJECT / ARRAY / STRING / BOOLEAN / NUMBER / INTEGER); [] là hợp lệ."""
    kind = (schema or {}).get('type', '')
    if kind == 'OBJECT':
        if not isinstance(answer, dict):
            return [f'{path}: cần object']
        errors = [f'{path}.{k}: thiếu' for k in schema.get('required', []) if k not in answer]
        for key, sub in (schema.get('properties') or {}).items():
            if key in answer:
                errors += validate(answer[key], sub, f'{path}.{key}')
        return errors
    if kind == 'ARRAY':
        if not isinstance(answer, list):
            return [f'{path}: cần array']
        return [e for i, item in enumerate(answer) for e in validate(item, schema.get('items') or {}, f'{path}[{i}]')]
    if kind == 'STRING':
        if not isinstance(answer, str):
            return [f'{path}: cần chuỗi']
        return [] if not schema.get('enum') or answer in schema['enum'] else [f'{path}: phải là một trong {schema["enum"]}']
    if kind == 'BOOLEAN':
        return [] if isinstance(answer, bool) else [f'{path}: cần boolean']
    if kind in ('NUMBER', 'INTEGER'):
        return [] if isinstance(answer, (int, float)) and not isinstance(answer, bool) else [f'{path}: cần số']
    return []


def output_schema(source, targets):
    """Schema file ra của một từ. Mỗi trường văn bản là object {mã ngôn ngữ: chuỗi} cho source + targets; translations chỉ có targets."""
    langs = [source, *targets]
    by_lang = lambda codes: {'type': 'OBJECT', 'properties': {c: {'type': 'STRING'} for c in codes}, 'required': list(codes)}
    strings = {'type': 'ARRAY', 'items': {'type': 'STRING'}}
    sense = {'type': 'OBJECT', 'required': ['pos', 'definition', 'usage', 'examples', 'translations', 'is_offensive', 'level', 'register', 'ipas', 'metadata', 'image'],
             'properties': {
                 'pos': {'type': 'STRING'}, 'definition': by_lang(langs), 'usage': by_lang(langs),
                 'examples': {'type': 'ARRAY', 'items': by_lang(langs)},
                 'translations': {'type': 'OBJECT', 'properties': {c: strings for c in targets}, 'required': list(targets)},
                 'is_offensive': {'type': 'BOOLEAN'}, 'level': {'type': 'STRING'}, 'register': strings,
                 'ipas': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'required': ['value', 'label'], 'properties': {'value': {'type': 'STRING'}, 'label': {'type': 'STRING'}}}},
                 'metadata': {'type': 'OBJECT', 'required': ['tags', 'image_keywords', *METADATA_LISTS],
                              'properties': {k: strings for k in ['tags', 'image_keywords', *METADATA_LISTS]}},
                 'image': {'type': 'OBJECT', 'required': ['kind'], 'properties': {'kind': {'type': 'STRING'}, 'subject': {'type': 'STRING'}, 'prompt': {'type': 'STRING'}}},
             }}
    return {'type': 'OBJECT', 'required': ['word', 'should_be_saved', 'senses'],
            'properties': {'word': {'type': 'STRING'}, 'should_be_saved': {'type': 'BOOLEAN'}, 'senses': {'type': 'ARRAY', 'items': sense}}}


def instructions(run):
    """Nội dung INSTRUCTIONS.md: quy tắc của các prompt trong luồng thường gộp thành một lần viết."""
    from utils.image_gen import brief
    source = run.lang
    targets = [c for c in run.state['base_languages'] if c != source]
    names = ', '.join(f'{c} ({language_map.get(c, c)})' for c in targets)
    entry_rules = render_word_prompt('<WORD>', source, [{'pos': '<pos>', 'glosses': ['<gloss>']}]).split('<reference>')[0]
    example = {
        'word': '<WORD>', 'should_be_saved': True,
        'senses': [{
            'pos': 'noun', 'definition': {c: '...' for c in [source, *targets]}, 'usage': {c: '' for c in [source, *targets]},
            'examples': [{c: '...' for c in [source, *targets]}], 'translations': {c: ['...'] for c in targets},
            'is_offensive': False, 'level': 'A1', 'register': ['neutral'], 'ipas': [{'value': '/.../', 'label': 'US'}],
            'metadata': {'tags': ['...'], 'image_keywords': ['...'], **{k: [] for k in METADATA_LISTS}},
            'image': {'kind': 'object', 'subject': '...', 'prompt': '...'}}]}
    return f"""# Viết dữ liệu từ vựng (một lần cho cả mục từ)

Mỗi từ có file `<từ>.in.json` ({{word, reference}}). Bạn viết `<từ>.out.json` cho từ đó (JSON hợp lệ, KHÔNG có markdown, không giải thích) rồi mới sang từ kế; xong từ nào ghi file từ đó.
Ngôn ngữ nguồn: {source} ({language_map.get(source, source)}). Ngôn ngữ dịch: {names}.

## 1. Mục từ ({source})
{entry_rules}

`reference` trong file vào (nếu có) là gợi ý chưa kiểm chứng: đi từng mục của nó, quyết định giữ hay bỏ theo đúng luật `Senses` và `reference` ở trên (như luồng thường), viết lại bằng lời của bạn.

## 2. Tự soát lại trước khi ghi (thay bước refine)
- Mỗi nghĩa có pos đúng; không gộp hai nghĩa khác nhau, không tách một nghĩa thành gần trùng; xoá nghĩa bịa hoặc trùng lặp. Không bỏ nghĩa chỉ vì ít dùng: giữ mọi nghĩa chuẩn mà từ điển học tập liệt kê, theo luật `Senses` ở mục 1.
- Định nghĩa không chứa chính từ đó; ví dụ chứa từ (hoặc dạng biến) và thể hiện đúng nghĩa đó, không dùng lại ví dụ ở nghĩa khác.
- `usage` để "" nếu không chắc.

## 3. Bản dịch ({', '.join(targets)})
- `definition`, `usage`, `examples`: dịch trung thành, tự nhiên, đúng nghĩa của nghĩa đó; không giải thích thêm. `usage` rỗng thì dịch cũng rỗng. Số phần tử `examples` bằng nguồn, cùng thứ tự.
- `translations[lang]`: 1-5 từ / cụm ngắn mà từ điển song ngữ liệt kê cho '<WORD>' Ở ĐÚNG NGHĨA NÀY, thông dụng nhất đứng trước; KHÔNG là câu, không là định nghĩa, không là từ của nghĩa khác, và PHẢI cùng loại từ với `pos` (nghĩa động từ cho động từ, danh từ cho danh từ). Với zh / ja / ko không dùng chữ Latin trong `translations`.
- Không để ký tự rộng 0; không thừa khoảng trắng đầu / cuối.

## 4. Metadata (mỗi nghĩa, chỉ về nghĩa đó)
- `synonyms` / `antonyms`: từ {source} cùng / ngược nghĩa. `relateds`: từ {source} gần nghĩa hoặc cùng họ từ. `collocations`: cụm thường gặp chứa từ, ví dụ "heavy rain". `idioms`: thành ngữ có từ này ở nghĩa này.
- `tags`: 1-5 tag chủ đề tiếng Anh viết thường. `image_keywords`: 1-3 từ khoá tiếng Anh tìm ảnh minh hoạ cụ thể.
- Tối đa 5 mục mỗi danh sách; để rỗng chứ đừng bịa hay lấy của nghĩa khác.

## 5. Brief ảnh minh hoạ (`image`, mỗi nghĩa)
Trả `kind`, `subject`, `prompt` (xem `image_kind`, `image_subject`, `image_prompt` bên dưới; `manual` thì để subject và prompt rỗng):
{brief.RULES}

## 6. Cấu trúc file ra (đúng khoá, đúng kiểu)
```json
{json.dumps(example, ensure_ascii=False, indent=1)}
```
- `should_be_saved`: false (và `senses` rỗng) nếu '<WORD>' không phải từ hợp lệ.
- `ipas`: cho ngôn ngữ nguồn en gồm US / UK; `register`: ["neutral"] nếu không đặc biệt.
"""


def export_words(run, directory):
    """Ghi INSTRUCTIONS.md + <từ>.in.json cho mọi từ 'new' (đã qua prepare). Trả số từ."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'INSTRUCTIONS.md').write_text(instructions(run), encoding='utf-8', newline='\n')
    words = run.words_in('new')
    for word in words:
        spec = {'word': word, 'reference': run.state['words'][word].get('reference') or []}
        (out / f'{file_stem(word)}.in.json').write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding='utf-8', newline='\n')
    return len(words)


def _brief_metadata(word, sense):
    """Brief của agent -> image_metadata dùng được, hoặc {} (để luồng viết lại online)."""
    from utils.image_gen import brief
    image = sense.get('image') or {}
    cleaned = brief._clean_brief(word, sense.get('pos'), {'image_kind': image.get('kind'), 'image_subject': image.get('subject'), 'image_prompt': image.get('prompt')})
    if not cleaned:
        return {}
    return {'subject': cleaned['subject'], 'prompt': cleaned['prompt'], 'kind': cleaned['kind'], 'source': brief.provider()}


def build_senses(run, word, data):
    """JSON của agent -> danh sách nghĩa cùng dạng với kết quả draft → refine → enrich (dùng chung với save_sense)."""
    source = run.lang
    targets = [c for c in run.state['base_languages'] if c != source]
    word_id = run.state['words'][word]['id']
    now = datetime.now(dt_timezone.utc).isoformat()
    senses = []
    for s in data['senses']:
        examples = [e for e in s['examples'] if (e.get(source) or '').strip()][:2]
        draft = {'pos': s['pos'], 'definition': {'value': s['definition'][source]}, 'usage': {'value': s['usage'].get(source, '')},
                 'examples': [{'value': e[source]} for e in examples], 'is_offensive': s['is_offensive'], 'level': s['level'],
                 'register': s['register'], 'ipas': s['ipas']}
        obj = build_sense_obj(draft, word_id, word, source)
        contents = obj['contents']
        for lang in targets:
            contents['definition'][lang] = {'value': s['definition'][lang]}
            if (s['usage'].get(source) or '').strip() and (s['usage'].get(lang) or '').strip():
                contents['usage'][lang] = {'value': s['usage'][lang]}
            for index, example in enumerate(examples):
                contents['examples'][index][lang] = {'value': example[lang]}
        contents['translations'] = {lang: list(s['translations'][lang]) for lang in targets}
        meta = s['metadata']
        obj['metadata'] = {'id': str(uuidv7.generate_uuid7()), 'tags': meta['tags'], 'image_keywords': meta['image_keywords'],
                           'image_metadata': _brief_metadata(word, s), 'advanced': {k: meta[k] for k in METADATA_LISTS if meta.get(k)}}
        obj['quality_status'] = 'refined'
        obj['ai_pass'] = 2
        obj['ai_model'] = MODEL_TAG
        obj['ai_review'] = {'unchanged': True, 'at': now, 'source': 'external'}
        senses.append(obj)
    return senses


def import_words(run, directory, sanitize, missing_enrichment):
    """
    Đọc <dir>/<từ>.out.json cho các từ 'new'. Hợp lệ -> run.senses + trạng thái 'enriched' (hoặc 'rejected' khi should_be_saved=false).
    Trả {'imported': [...], 'rejected': [...], 'invalid': [(từ, lỗi)], 'missing': [từ]}.
    """
    directory = Path(directory)
    schema = output_schema(run.lang, [c for c in run.state['base_languages'] if c != run.lang])
    report = {'imported': [], 'rejected': [], 'invalid': [], 'missing': []}
    for word in run.words_in('new'):
        path = directory / f'{file_stem(word)}.out.json'
        if not path.exists():
            report['missing'].append(word)
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except ValueError as e:
            report['invalid'].append((word, f'JSON hỏng: {e}'))
            continue
        errors = validate(data, schema)
        if errors:
            report['invalid'].append((word, '; '.join(errors[:4])))
            continue
        if not data['should_be_saved'] or not data['senses']:
            run.mark(word, 'rejected', 'agent: should_be_saved=false')
            report['rejected'].append(word)
            continue
        senses = build_senses(run, word, data)
        fixed = sanitize(senses, run.lang)
        run.state.setdefault('sanitized', {})[word] = {k: v for k, v in fixed.items() if v}
        gaps = missing_enrichment(senses, run.lang, run.state['base_languages'])
        run.senses[word] = senses
        run.state['words'][word]['external'] = True
        run.mark(word, 'enriched', '; '.join(gaps))
        if gaps:
            run.state['issues'].append({'word': word, 'stage': 'external', 'missing': gaps})
        report['imported'].append(word)
    run.save()
    return report


# ----------------------------------------------------------------- verify bằng agent ngoài (thay lệnh Claude ở stage verify)

def export_verify(run, directory, redo=False, set_flag=True):
    """
    Ghi <dir>/<từ>.verify.in.json = {word, custom_id, system, prompt, schema} cho từ đã lưu (cùng prompt mà stage verify gửi Haiku).
    set_flag: đặt run.state['agent_verify'] để stage verify chờ file kết quả thay vì gọi API. Trả số từ.
    """
    from ai.seedflow import verify_db, verify
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    ids = [run.state['words'][w]['id'] for w in run.words_in('saved')]
    requests, meta = verify_db.build_requests(ids, run.lang, run.state['base_languages'], redo=redo)
    for request in requests:
        word = meta[request['custom_id']][0]
        spec = {'word': word, 'custom_id': request['custom_id'], 'system': verify.SYSTEM,
                'prompt': request['params']['messages'][0]['content'], 'schema': verify.SCHEMA}
        (out / f'{file_stem(word)}.verify.in.json').write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding='utf-8', newline='\n')
    if set_flag:
        run.state['agent_verify'] = True
        run.save()
    return len(requests)


def import_verify(run, directory):
    """Gộp <dir>/<từ>.verify.out.json hợp lệ vào seed_runs/<run>/verify_external.json (dạng kết quả Claude). Trả {'ok','invalid','missing'}."""
    from ai.seedflow import verify
    results, invalid, missing = {}, [], []
    for in_path in sorted(Path(directory).glob('*.verify.in.json')):
        spec = json.loads(in_path.read_text(encoding='utf-8'))
        out_path = in_path.with_name(in_path.name.replace('.verify.in.json', '.verify.out.json'))
        if not out_path.exists():
            missing.append(spec['word'])
            continue
        try:
            data = json.loads(out_path.read_text(encoding='utf-8'))
        except ValueError as e:
            invalid.append((spec['word'], f'JSON hỏng: {e}'))
            continue
        errors = validate(data, verify.SCHEMA)
        if errors:
            invalid.append((spec['word'], '; '.join(errors[:4])))
            continue
        results[spec['custom_id']] = {'ok': True, 'text': json.dumps(data, ensure_ascii=False), 'stop_reason': 'end_turn', 'tokens_in': 0, 'tokens_out': 0, 'error': ''}
    (run.dir / 'verify_external.json').write_text(json.dumps(results, ensure_ascii=False), encoding='utf-8', newline='\n')
    return {'ok': len(results), 'invalid': invalid, 'missing': missing}


# ----------------------------------------------------------------- judge concept bằng agent ngoài

def concept_key(head, definition):
    return f'{head}|{definition}'


def export_concept(run, directory, set_flag=True):
    """
    Với từng nghĩa đã lưu chưa có concept: tìm ứng viên (embedding + so cosine, chỉ đọc DB) rồi ghi <dir>/<từ>.concept.in.json = {word, system, tasks:[{id, key, prompt}]}.
    Chỉ nghĩa có ứng viên mới cần judge (không có thì tạo concept mới, không cần ai quyết). Embedding vẫn gọi API Gemini (rẻ, agent không tạo vector được).
    """
    from ai.models.AISense import AISense
    from utils.concept import linker, text
    from utils.concept.embedding import embed
    from utils.concept.judge import SYSTEM, _lines
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    pool = linker._load_pool()
    total = 0
    for word in run.words_in('saved'):
        tasks = []
        for sense in AISense.objects.filter(word_id=run.state['words'][word]['id'], is_official=True, concept__isnull=True).order_by('created_at', 'id'):
            definition = text.en_definition(sense)
            if not definition:
                continue
            cands, _ = linker._candidates(sense, embed([text.embed_text(sense)])[0], pool)
            if not cands:
                continue
            head = text.head(sense)
            tasks.append({'id': str(sense.id), 'key': concept_key(head, definition), 'candidate_ids': [c['id'] for c in cands],
                          'prompt': f"Sense: gloss \"{head}\" [{sense.pos or ''}]: {definition}\nCandidates:\n{_lines(cands)}", 'candidates': len(cands)})
        if tasks:
            (out / f'{file_stem(word)}.concept.in.json').write_text(json.dumps({'word': word, 'system': SYSTEM, 'tasks': tasks}, ensure_ascii=False, indent=1), encoding='utf-8', newline='\n')
            total += len(tasks)
    if set_flag:
        run.state['agent_concept'] = True
        run.save()
    return total


def import_concept(run, directory):
    """Gộp <dir>/<từ>.concept.out.json = {id nghĩa: {choice, near}} vào seed_runs/<run>/concept_external.json. Trả {'ok','invalid','missing'}."""
    decisions, invalid, missing = {}, [], []
    for in_path in sorted(Path(directory).glob('*.concept.in.json')):
        spec = json.loads(in_path.read_text(encoding='utf-8'))
        out_path = in_path.with_name(in_path.name.replace('.concept.in.json', '.concept.out.json'))
        if not out_path.exists():
            missing.append(spec['word'])
            continue
        try:
            answers = json.loads(out_path.read_text(encoding='utf-8'))
        except ValueError as e:
            invalid.append((spec['word'], f'JSON hỏng: {e}'))
            continue
        for task in spec['tasks']:
            answer = (answers or {}).get(task['id'])
            ids = task['candidate_ids']
            choice = answer.get('choice') if isinstance(answer, dict) else None
            near = answer.get('near') if isinstance(answer, dict) else None
            if not isinstance(choice, int) or isinstance(choice, bool) or not 0 <= choice <= len(ids) or not isinstance(near, list) \
                    or any(not isinstance(n, int) or not 1 <= n <= len(ids) for n in near):
                invalid.append((spec['word'], f"nghĩa {task['id']}: câu trả lời sai"))
                continue
            decisions[task['key']] = {'concept_id': ids[choice - 1] if choice else None, 'near': [ids[n - 1] for n in near if n != choice], 'cands': ids}
    (run.dir / 'concept_external.json').write_text(json.dumps(decisions, ensure_ascii=False), encoding='utf-8', newline='\n')
    return {'ok': len(decisions), 'invalid': invalid, 'missing': missing}


def concept_judge(decisions, fallback):
    """
    judge_fn cho linker: dùng quyết định của agent khi ứng viên hiện tại là tập con của ứng viên lúc xuất (nghĩa cùng từ đã nối trước đó bị loại khỏi
    danh sách thì không sao); ứng viên mới xuất hiện hay không có quyết định thì gọi `fallback` (judge cấu hình sẵn). Trả (hàm, bộ đếm).
    """
    counters = {'agent': 0, 'fallback': 0}

    def judge(sense_info, candidates, runs=None):
        decision = decisions.get(concept_key(sense_info['head'], sense_info['definition']))
        current = {c['id'] for c in candidates}
        if decision and current <= set(decision['cands']):
            counters['agent'] += 1
            chosen = decision['concept_id'] if decision['concept_id'] in current else None
            return {'concept_id': chosen, 'near': [n for n in decision['near'] if n in current], 'runs': [{'chosen': chosen, 'near': decision['near']}], 'agree': True}
        counters['fallback'] += 1
        return fallback(sense_info, candidates, runs)

    return judge, counters
