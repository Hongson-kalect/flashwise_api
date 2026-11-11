"""Lọc dạng chia khỏi danh sách nguồn (ai/seedflow/lemmas.py)."""
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from ai.seedflow import lemmas
from utils.morph import Analysis

TABLE = {
    'went': [Analysis('go', 'verb', 'verb.past')],
    'men': [Analysis('man', 'noun', 'noun.plural')],
    'lives': [Analysis('life', 'noun', 'noun.plural'), Analysis('live', 'verb', 'verb.3sg')],
    'killed': [Analysis('kill', 'verb', 'verb.past')],   # kill không có trong nguồn: giữ killed
    'saw': [Analysis('see', 'verb', 'verb.past')],
    'running': [Analysis('run', 'verb', 'verb.ing')],
    'gone': [Analysis('go', 'verb', 'verb.pp')],
    'number': [Analysis('numb', 'adj', 'adj.comparative')],
    'later': [Analysis('late', 'adj', 'adj.comparative')],
    'opening': [Analysis('open', 'verb', 'verb.ing')],
    'gotta': [],
}


def analyze(word, lang):
    return TABLE.get(word, [])


def rows(*words, pos=None, senses=None):
    """pos: {từ: [loại từ]} (mặc định ['noun']); senses: {từ: số nghĩa riêng ở nguồn} (mặc định 3)."""
    pos, senses = pos or {}, senses or {}
    out = []
    for i, w in enumerate(words):
        data = {'w': w, 'rank': i + 1, 'n_senses': senses.get(w, 3), 'entries': [{'pos': p, 'glosses': ['x']} for p in pos.get(w, ['noun'])]}
        out.append((data, json.dumps(data)))
    return out


class SplitFormsTests(SimpleTestCase):
    def split(self, *words, pos=None, senses=None, **kw):
        return lemmas.split_forms(rows(*words, pos=pos, senses=senses), 'en', analyze_fn=analyze, **kw)

    def test_drops_forms_whose_lemma_is_in_source(self):
        kept, dropped, lexical = self.split('go', 'went', 'man', 'men', 'killed', 'run', pos={'went': ['verb'], 'men': ['noun']})
        self.assertEqual([d['w'] for d, _ in kept], ['go', 'man', 'killed', 'run'])
        self.assertEqual([(d['form'], d['lemma'], d['tags']) for d in dropped], [('went', 'go', 'verb.past'), ('men', 'man', 'noun.plural')])
        self.assertEqual(lexical, [])

    def test_lemma_missing_from_source_keeps_form(self):
        kept, dropped, _ = self.split('killed', 'run')
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_form_with_own_pos_beyond_analysis_is_kept_and_reported(self):
        kept, dropped, lexical = self.split('see', 'saw', 'go', 'gone', pos={'saw': ['noun', 'verb'], 'gone': ['adj', 'prep']})
        self.assertEqual([d['w'] for d, _ in kept], ['see', 'saw', 'go', 'gone'])
        self.assertEqual(dropped, [])
        self.assertEqual([(l['form'], l['analysis_pos'], l['own_pos'], l['reason']) for l in lexical],
                         [('saw', 'adj,verb', 'noun,verb', 'pos'), ('gone', 'adj,verb', 'adj,prep', 'pos')])

    def test_derived_pos_of_ing_and_participle_forms_is_covered(self):
        kept, dropped, _ = self.split('run', 'running', pos={'running': ['noun', 'adj']})  # gerund + tính từ phân từ
        self.assertEqual([d['form'] for d in dropped], ['running'])
        kept, dropped, lexical = self.split('go', 'went', pos={'went': ['verb', 'noun']})  # danh từ không phải phái sinh của quá khứ
        self.assertEqual(dropped, [])
        self.assertEqual([l['form'] for l in lexical], ['went'])

    def test_wrong_analysis_with_other_pos_kept(self):
        kept, dropped, lexical = self.split('numb', 'number', pos={'number': ['noun', 'verb']})
        self.assertEqual(len(kept), 2)
        self.assertEqual([l['form'] for l in lexical], ['number'])

    def test_comparative_covers_adverb(self):
        kept, dropped, _ = self.split('late', 'later', pos={'later': ['adj', 'adverb']})
        self.assertEqual([d['form'] for d in dropped], ['later'])

    def test_min_senses_keeps_rich_independent_forms(self):
        kept, dropped, lexical = self.split('open', 'opening', pos={'opening': ['noun']}, senses={'opening': 16}, min_senses=8)
        self.assertEqual(dropped, [])
        self.assertEqual([(l['form'], l['reason']) for l in lexical], [('opening', 'senses')])
        kept, dropped, lexical = self.split('open', 'opening', pos={'opening': ['noun']}, senses={'opening': 16})  # tắt mặc định
        self.assertEqual([d['form'] for d in dropped], ['opening'])

    def test_row_without_entries_or_unknown_analysis_pos_uses_basic_rule(self):
        no_entries = [({'w': w, 'rank': i + 1}, json.dumps({'w': w, 'rank': i + 1})) for i, w in enumerate(['go', 'went'])]
        kept, dropped, _ = lemmas.split_forms(no_entries, 'en', analyze_fn=analyze)
        self.assertEqual([d['form'] for d in dropped], ['went'])
        unknown = lambda w, l: [Analysis('go', None, 'form')] if w == 'went' else []
        kept, dropped, _ = lemmas.split_forms(rows('go', 'went', pos={'went': ['noun']}), 'en', analyze_fn=unknown)
        self.assertEqual([d['form'] for d in dropped], ['went'])

    def test_ambiguous_form_picks_most_frequent_lemma_and_lists_others(self):
        kept, dropped, _ = self.split('life', 'live', 'lives', pos={'lives': ['noun', 'verb']})
        self.assertEqual([d['form'] for d in dropped], ['lives'])
        self.assertEqual((dropped[0]['lemma'], dropped[0]['others'], dropped[0]['lemma_rank']), ('life', 'live', 1))

    def test_keep_list_and_phrases_untouched(self):
        kept, dropped, _ = self.split('see', 'saw', 'go ahead', pos={'saw': ['verb']}, keep=['saw'])
        self.assertEqual([d['w'] for d, _ in kept], ['see', 'saw', 'go ahead'])
        self.assertEqual(dropped, [])

    def test_keeps_original_rank_and_writes_files(self):
        kept, dropped, lexical = self.split('go', 'went', 'run', pos={'went': ['verb']})
        out = Path(tempfile.mkdtemp()) / 'x' / 'lemmas.jsonl'
        table = lemmas.write_outputs(kept, dropped, lexical, out)
        lines = [json.loads(l) for l in out.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(l['w'], l['rank']) for l in lines], [('go', 1), ('run', 3)])  # rank gốc giữ nguyên
        self.assertIn('went\t2\tgo\t1\tverb.past', table.read_text(encoding='utf-8'))
        self.assertTrue(out.with_name('lemmas.jsonl.kept_forms.tsv').exists())
