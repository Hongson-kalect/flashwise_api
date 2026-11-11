"""
Chia danh sách từ nguồn thành các "step": mỗi step là 1 file JSONL (step_0001.jsonl, ...) cho luồng seed.

- word_source: file xếp theo độ thông dụng giảm dần. Nhận .jsonl (mỗi dòng {"w", "rank", "n_senses", "entries"} như words_ref.jsonl của
  flashwise/data/seed) hoặc .txt / .tsv (cột đầu là từ; dòng đầu "rank<TAB>word..." thì lấy cột `word`).
- Từ nhiều nghĩa (Wiktionary >= multi_sense_threshold nghĩa, mặc định 40) KHÔNG vào step: đưa sang multi_sense.jsonl để thêm sau bằng admin,
  vì AI liệt kê hết sẽ tràn token ra (8192) hoặc đẻ nghĩa hiếm.
- Từ đã có trong DB (xong / đang tạo / bị từ chối) bỏ qua; từ không phải mục từ bị bỏ.
- manifest.json nhớ con trỏ (hạng nguồn đã duyệt) nên chạy step k+1 tiếp ngay sau step k; chạy lại step đã có thì trả lại file cũ.
"""
import json
from pathlib import Path

MULTI_SENSE_THRESHOLD = 40  # hạ từ 50 (2026-11-11): nhiều nghĩa Wiktionary là nghĩa hiếm; step đã tạo trước đó giữ nguyên
DONE_STATUSES = ('COMPLETED', 'PROCESSING', 'REJECTED', 'BANNED')


def read_source(path):
    """[{'w', 'rank', 'n_senses', 'reference'}] theo thứ tự file."""
    path = Path(path)
    rows = []
    if path.suffix == '.jsonl':
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get('w'):
                entries = [{k: v for k, v in e.items() if k != 'n'} for e in (r.get('entries') or []) if isinstance(e, dict)]
                rows.append({'w': r['w'], 'rank': r.get('rank') or len(rows) + 1, 'n_senses': r.get('n_senses'), 'reference': entries})
        return rows
    column = 0
    for i, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines()):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        cells = line.split('\t')
        if i == 0 and cells[0] == 'rank' and 'word' in cells:
            column = cells.index('word')
            continue
        rows.append({'w': cells[column].strip(), 'rank': len(rows) + 1, 'n_senses': None, 'reference': []})
    return rows


def existing_words(words, language_code):
    from ai.models import AIWord
    return set(AIWord.objects.filter(language_code=language_code, value__in=list(words), status__in=DONE_STATUSES).values_list('value', flat=True))


def make_step_file(word_source, out_dir, number_per_step, language_code='en', step=None, threshold=MULTI_SENSE_THRESHOLD,
                   existing_fn=existing_words, is_word_fn=None):
    """
    Tạo step_NNNN.jsonl với `number_per_step` từ kế tiếp (sau khi bỏ từ nhiều nghĩa / đã có / không hợp lệ). Trả dict mô tả step.
    step: số thứ tự muốn tạo; None = step kế tiếp. Chỉ tạo được step kế tiếp (con trỏ tuần tự), step đã có thì trả lại bản cũ.
    """
    from utils.utils.normalize import is_lookup_word, normalize_lookup
    is_word_fn = is_word_fn or is_lookup_word
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {
        'source': str(word_source), 'language_code': language_code, 'cursor': 0, 'steps': []}
    if manifest['language_code'] != language_code:
        raise ValueError(f"Thư mục {out} dùng cho ngôn ngữ {manifest['language_code']}")
    if step is not None:
        for s in manifest['steps']:
            if s['step'] == step:
                return s
        if step != len(manifest['steps']) + 1:
            raise ValueError(f"Chỉ tạo được step {len(manifest['steps']) + 1} (đã có {len(manifest['steps'])})")
    if number_per_step < 1:
        raise ValueError('number_per_step phải >= 1')

    rows = read_source(word_source)
    multi_path = out / 'multi_sense.jsonl'
    multi_seen = set()
    if multi_path.exists():
        multi_seen = {json.loads(l)['w'] for l in multi_path.read_text(encoding='utf-8').splitlines() if l.strip()}

    picked, multi_new, skipped = [], [], {'existing': 0, 'invalid': 0, 'duplicate': 0}
    seen_in_step = set()
    candidates = [r for r in rows if r['rank'] > manifest['cursor']]
    last_rank = manifest['cursor']
    for start in range(0, len(candidates), 500):
        if len(picked) >= number_per_step:
            break
        chunk = candidates[start:start + 500]
        known = existing_fn([normalize_lookup(r['w']) for r in chunk], language_code)  # 1 query mỗi 500 từ
        for r in chunk:
            if len(picked) >= number_per_step:
                break
            last_rank = r['rank']
            word = normalize_lookup(r['w'])
            if not is_word_fn(word):
                skipped['invalid'] += 1
            elif word in seen_in_step:
                skipped['duplicate'] += 1
            elif r['n_senses'] and r['n_senses'] >= threshold:
                if word not in multi_seen:
                    multi_new.append({'w': word, 'rank': r['rank'], 'n_senses': r['n_senses']})
                    multi_seen.add(word)
            elif word in known:
                skipped['existing'] += 1
            else:
                seen_in_step.add(word)
                picked.append({'w': word, 'rank': r['rank'], 'n_senses': r['n_senses'], 'reference': r['reference']})
    if not picked:
        raise ValueError('Hết từ trong nguồn: không còn từ nào để tạo step')

    number = len(manifest['steps']) + 1
    name = f'step_{number:04d}.jsonl'
    (out / name).write_text(''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in picked), encoding='utf-8', newline='\n')
    if multi_new:
        with open(multi_path, 'a', encoding='utf-8', newline='\n') as f:
            f.writelines(json.dumps(m, ensure_ascii=False) + '\n' for m in multi_new)
    info = {'step': number, 'file': name, 'count': len(picked), 'from_rank': picked[0]['rank'], 'to_rank': last_rank,
            'multi_sense_moved': len(multi_new), 'skipped': skipped}
    manifest['cursor'] = last_rank
    manifest['steps'].append(info)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    return info


def read_step(path):
    return [json.loads(l) for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip()]
