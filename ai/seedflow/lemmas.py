"""
Lọc dạng biến cách khỏi danh sách nguồn, chỉ giữ từ gốc, bằng hàm nhận dạng sẵn có của từng ngôn ngữ (utils/morph.analyze: lemminflect cho en,
pymorphy3 cho ru, fugashi cho ja, kiwipiepy cho ko, simplemma cho phần còn lại; ngôn ngữ không biến cách thì không loại gì).

Một dòng bị loại khi thoả CẢ BA:
  1. analyze(từ) cho ra ít nhất một từ gốc KHÁC chính nó (went -> go, men -> man);
  2. từ gốc đó cũng có trong danh sách nguồn (không thì giữ dạng đó, tránh mất cả hai);
  3. mọi loại từ mà dòng có ở nguồn (`entries[].pos`) đều là loại từ mà phép phân tích giải thích được, kể cả cách dùng phái sinh quen thuộc
     (DERIVED_POS: dạng -ing còn là danh từ gerund / tính từ phân từ, dạng -ed / quá khứ phân từ còn là tính từ, so sánh còn là trạng từ).
Điều kiện 3 giữ lại dạng có nghĩa riêng ở loại từ mà phép phân tích không giải thích được: saw (danh từ: cái cưa), ground (danh từ; phân tích sai thành quá khứ
của grind), number (danh từ; phân tích sai thành so sánh của numb), best, left, bit, shot (danh từ), gone (giới từ), days (trạng từ).
Dạng chỉ có nghĩa phái sinh (saying, looks, thinking, working, killed, lost) vẫn bị loại: danh sách `keep` giữ lại, bảng `dropped` để xem lại.
Dòng không có `entries` hoặc phân tích không biết loại từ (ngôn ngữ dùng simplemma) thì chỉ xét điều kiện 1 và 2.

Cụm nhiều từ không động tới. Giữ nguyên `rank` gốc và mọi trường của dòng nên con trỏ của `make-step` vẫn dùng tiếp được.
Xem bảng bị loại (`dropped`) và bảng dạng được giữ vì có nghĩa riêng (`kept_forms`) trước khi seed.
"""
import json
from pathlib import Path

from utils import morph


def read_rows(path):
    """Các dòng JSON của nguồn .jsonl theo thứ tự file, giữ nguyên (dict đầy đủ) và dòng gốc để ghi lại không đổi."""
    rows = []
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if data.get('w'):
            rows.append((data, line))
    return rows


# Loại từ phái sinh quen thuộc mà một dạng chia thường có thêm (theo nhãn của morph.analyze, tiếng Anh): coi là đã được từ gốc bao phủ
DERIVED_POS = {
    'verb.ing': {'noun', 'adj'},
    'verb.past': {'adj'},
    'verb.pp': {'adj'},
    'adj.comparative': {'adv'},
    'adj.superlative': {'adv'},
    'adv.comparative': {'adj'},
    'adv.superlative': {'adj'},
}


def _own_pos(data):
    """Tập loại từ của dòng nguồn (noun / verb / adj / adv, loại khác giữ nguyên chữ); rỗng nếu nguồn không có `entries`."""
    out = set()
    for entry in data.get('entries') or []:
        pos = (entry.get('pos') or '').strip().lower()
        if pos:
            out.add(morph.normalize_pos(pos) or pos)
    return out


def split_forms(rows, lang, analyze_fn=morph.analyze, keep=(), min_senses=0):
    """
    rows: [(dict, dòng gốc)]. Trả (giữ lại, bị loại, giữ vì có nghĩa riêng):
      giữ lại: [(dict, dòng gốc)]; bị loại: [{'form', 'rank', 'lemma', 'lemma_rank', 'tags', 'others', 'n_senses'}];
      giữ vì có nghĩa riêng: [{'form', 'rank', 'lemma', 'analysis_pos', 'own_pos', 'n_senses', 'reason'}] (đã có từ gốc trong nguồn nhưng giữ vì loại từ riêng khác ('pos') hoặc nhiều nghĩa riêng ('senses')).
    keep: các từ luôn giữ dù là dạng chia. min_senses > 0: dạng có từ ngần này nghĩa riêng ở nguồn trở lên cũng giữ (opening, reading, setting: danh từ độc lập nhiều nghĩa).
    """
    keep = {k.strip().lower() for k in keep if k.strip()}
    rank_of = {}
    for data, _ in rows:
        rank_of.setdefault(data['w'].lower(), data.get('rank'))
    kept, dropped, lexical = [], [], []
    for data, line in rows:
        word = data['w'].lower()
        if word in keep or ' ' in word.strip():
            kept.append((data, line))
            continue
        lemmas = {}
        pos_covered = set()  # loại từ mà phép phân tích giải thích được (None = không biết loại từ)
        for guess in analyze_fn(word, lang):
            lemma = (guess.lemma or '').lower()
            if lemma and lemma != word and lemma in rank_of:
                lemmas.setdefault(lemma, []).append(guess.tag)
                pos_covered.add(guess.pos)
                pos_covered.update(DERIVED_POS.get(guess.tag, ()))
        if not lemmas:
            kept.append((data, line))
            continue
        own = _own_pos(data)
        best = min(lemmas, key=lambda l: (rank_of[l] if rank_of[l] is not None else 10 ** 9, l))  # từ gốc thông dụng nhất làm đại diện
        reason = 'pos' if own and None not in pos_covered and not own <= pos_covered else (
            'senses' if min_senses and (data.get('n_senses') or 0) >= min_senses else None)
        if reason:
            kept.append((data, line))
            lexical.append({'form': data['w'], 'rank': data.get('rank'), 'lemma': best, 'analysis_pos': ','.join(sorted(x for x in pos_covered if x)),
                            'own_pos': ','.join(sorted(own)), 'n_senses': data.get('n_senses'), 'reason': reason})
            continue
        dropped.append({
            'form': data['w'], 'rank': data.get('rank'), 'lemma': best, 'lemma_rank': rank_of[best],
            'tags': ','.join(sorted({t for tags in lemmas.values() for t in tags})), 'others': ','.join(l for l in lemmas if l != best),
            'n_senses': data.get('n_senses'),
        })
    return kept, dropped, lexical


def _tsv(path, cols, rows):
    path.write_text('\t'.join(cols) + '\n' + ''.join('\t'.join(str(r[c] if r[c] is not None else '') for c in cols) + '\n' for r in rows),
                    encoding='utf-8', newline='\n')


def write_outputs(kept, dropped, lexical, out_path):
    """Ghi danh sách giữ lại (.jsonl, cùng dạng nguồn), bảng bị loại (<out>.dropped.tsv) và bảng dạng được giữ vì có nghĩa riêng (<out>.kept_forms.tsv)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(''.join(line + '\n' for _, line in kept), encoding='utf-8', newline='\n')
    dropped_table = out.with_name(out.name + '.dropped.tsv')
    _tsv(dropped_table, ['form', 'rank', 'lemma', 'lemma_rank', 'tags', 'others', 'n_senses'], dropped)
    _tsv(out.with_name(out.name + '.kept_forms.tsv'), ['form', 'rank', 'lemma', 'analysis_pos', 'own_pos', 'n_senses', 'reason'], lexical)
    return dropped_table
