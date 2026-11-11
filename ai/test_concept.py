"""Concept tầng 1 (utils/concept): nối nghĩa giống hệt giữa ngôn ngữ, tách khi nghi ngờ, ghi quyết định, chia sẻ ảnh, payload cho app."""
from unittest import mock

import numpy as np
from django.test import SimpleTestCase, TestCase, override_settings

from ai.models import AIWord
from ai.models.AISense import AISense
from ai.models.Concept import Concept, ConceptDecision
from core.models.ImageLibrary import ImageContext
from utils.concept import judge as judge_module
from utils.concept import linker, text
from utils.concept.payload import concept_payload

DIM = 8


def unit(*parts):
    v = np.zeros(DIM, dtype=np.float32)
    for i, x in parts:
        v[i] = x
    return v / np.linalg.norm(v)


# Mỗi "ý" là một trục; nhiễu nhẹ để cùng ý không bằng đúng 1.0
VECS = {
    'apple': unit((0, 1.0), (7, 0.05)), 'manzana': unit((0, 1.0), (7, 0.1)), 'pomme': unit((0, 1.0), (6, 0.1)),
    'bank': unit((1, 1.0)), 'bank_money': unit((1, 1.0)), 'banco_money': unit((1, 1.0), (7, 0.1)), 'bank_river': unit((2, 1.0), (1, 0.55)),
    'huge': unit((3, 1.0)), 'vast': unit((3, 1.0), (4, 0.6)), 'hello': unit((5, 1.0)), 'hi': unit((5, 1.0), (7, 0.1)),
    'sobremesa': unit((4, 0.3), (6, 1.0)),
}


def fake_embed(texts):
    # khoá là chữ đứng trước dấu ':' (head) hoặc 'head#loại' ghi trong test
    out = []
    for t in texts:
        key = t.split(':')[0].strip().split(',')[0].strip()
        out.append(VECS[key])
    return np.vstack(out)


def make_sense(value, lang, definition, gloss=None, pos='noun'):
    word = AIWord.objects.create(value=value, language_code=lang, status='COMPLETED')
    contents = {'definition': {'en': {'value': definition}}, 'translations': {'en': gloss if gloss is not None else ([] if lang == 'en' else [value])}}
    return AISense.objects.create(word=word, word_value=value, language_code=lang, contents=contents, pos=pos)


def judge_picks(mapping):
    """judge giả: chọn concept có nhãn trong mapping[head] (None = NEW)."""
    def fn(info, candidates):
        want = mapping.get(info['head'])
        for c in candidates:
            if c['label'] == want:
                return {'concept_id': c['id'], 'near': [], 'runs': [], 'agree': True}
        return {'concept_id': None, 'near': [c['id'] for c in candidates], 'runs': [], 'agree': True}
    return fn


@override_settings(CONCEPT_MIN_COS=0.7)
class LinkerTests(TestCase):
    def link(self, sense, mapping=None, judge_fn=None):
        return linker.link_sense(sense, judge_fn=judge_fn or judge_picks(mapping or {}), embed_fn=fake_embed)

    def test_first_english_sense_creates_anchored_concept(self):
        r = self.link(make_sense('apple', 'en', 'A round fruit.'))
        c = Concept.objects.get()
        self.assertEqual((r['outcome'], c.status, c.label, c.member_count), ('created', Concept.ANCHORED, 'apple', 1))

    def test_foreign_sense_links_to_english_concept_and_logs_decision(self):
        self.link(make_sense('apple', 'en', 'A round fruit.'))
        s = make_sense('manzana', 'es', 'A round fruit.', gloss=['manzana'])
        # gloss en là "manzana" nên head cũng "manzana" -> VECS có khoá này
        r = self.link(s, {'manzana': 'apple'})
        s.refresh_from_db()
        self.assertEqual(r['outcome'], 'linked')
        self.assertEqual(s.concept.label, 'apple')
        self.assertEqual(Concept.objects.count(), 1)
        self.assertEqual(Concept.objects.get().member_count, 2)
        d = ConceptDecision.objects.filter(sense=s).get()
        self.assertEqual((d.outcome, d.concept_id), ('linked', s.concept_id))
        self.assertGreaterEqual(d.cos, 0.7)

    def test_judge_new_keeps_senses_apart(self):
        self.link(make_sense('bank', 'en', 'A place that keeps money.'))
        # vector gần nhưng judge nói NEW
        river = make_sense('bank_river', 'en', 'The land beside a river.')
        r = self.link(river, {})
        self.assertEqual(r['outcome'], 'created')
        self.assertEqual(Concept.objects.count(), 2)

    def test_same_word_other_sense_is_never_a_candidate(self):
        word = AIWord.objects.create(value='bank', language_code='en', status='COMPLETED')
        a = AISense.objects.create(word=word, word_value='bank', language_code='en', contents={'definition': {'en': 'A place that keeps money.'}, 'translations': {'en': []}}, pos='noun')
        b = AISense.objects.create(word=word, word_value='bank_money', language_code='en', contents={'definition': {'en': 'Also money.'}, 'translations': {'en': []}}, pos='noun')
        self.link(a)
        seen = {}

        def spy(info, cands):
            seen['cands'] = cands
            return {'concept_id': None, 'near': [], 'runs': [], 'agree': True}
        self.link(b, judge_fn=spy)
        self.assertNotIn('cands', seen)  # concept duy nhất đã có nghĩa của cùng từ nên bị loại, không gọi judge

    def test_disagreeing_judge_runs_split_and_record_near(self):
        self.link(make_sense('huge', 'en', 'Very big.'))
        other = make_sense('vast', 'en', 'Extremely large in area.')
        cid = str(Concept.objects.get().id)
        r = self.link(other, judge_fn=lambda info, c: {'concept_id': None, 'near': [cid], 'runs': [{'chosen': cid}, {'chosen': None}], 'agree': False})
        self.assertEqual(r['outcome'], 'ambiguous_split')
        d = ConceptDecision.objects.get(sense=other)
        self.assertEqual(d.near, [cid])
        self.assertEqual(Concept.objects.count(), 2)

    def test_foreign_without_gloss_is_lexical_gap(self):
        r = self.link(make_sense('sobremesa', 'es', 'Time spent talking at the table after a meal.', gloss=[]))
        c = Concept.objects.get()
        self.assertEqual((r['outcome'], c.status), ('created_gap', Concept.LEXICAL_GAP))

    def test_provisional_becomes_anchored_when_english_arrives(self):
        self.link(make_sense('pomme', 'fr', 'A round fruit.', gloss=['pomme']))
        self.assertEqual(Concept.objects.get().status, Concept.PROVISIONAL)
        english = make_sense('apple', 'en', 'A round fruit.')
        r = self.link(english, {'apple': 'pomme'})
        c = Concept.objects.get()
        self.assertEqual((r['outcome'], c.status, c.label, c.member_count), ('linked', Concept.ANCHORED, 'apple', 2))

    def test_sense_without_english_definition_is_skipped(self):
        s = make_sense('xyz', 'es', '')
        r = self.link(s)
        self.assertEqual(r['outcome'], 'skipped')
        self.assertEqual(Concept.objects.count(), 0)

    def test_already_linked_sense_is_left_alone(self):
        s = make_sense('apple', 'en', 'A round fruit.')
        self.link(s)
        s.refresh_from_db()
        self.assertEqual(self.link(s)['outcome'], 'already')
        self.assertEqual(ConceptDecision.objects.count(), 1)

    def test_images_are_shared_in_both_directions(self):
        a = make_sense('apple', 'en', 'A round fruit.')
        ctx = ImageContext.objects.create(provider='test', provider_id='p1', description='apple')
        AISense.objects.filter(id=a.id).update(image_preview='images/v1/apple.webp', image_context=ctx)
        a.refresh_from_db()
        self.link(a)
        b = make_sense('manzana', 'es', 'A round fruit.', gloss=['manzana'])
        r = self.link(b, {'manzana': 'apple'})
        b.refresh_from_db()
        self.assertEqual((b.image_preview, b.image_context_id), ('images/v1/apple.webp', ctx.id))
        self.assertEqual(r['image_changed'], [str(b.id)])
        # chiều ngược: nghĩa mới có ảnh, anh em chưa có
        c = make_sense('pomme', 'fr', 'A round fruit.', gloss=['pomme'])
        ctx2 = ImageContext.objects.create(provider='test', provider_id='p2', description='pomme')
        AISense.objects.filter(id=c.id).update(image_preview='images/v1/pomme.webp', image_context=ctx2)
        AISense.objects.filter(id=a.id).update(image_preview=None, image_context=None)
        AISense.objects.filter(id=b.id).update(image_preview=None, image_context=None)
        c.refresh_from_db()
        self.link(c, {'pomme': 'apple'})
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.image_preview, b.image_preview), ('images/v1/pomme.webp', 'images/v1/pomme.webp'))

    def test_payload_lists_equivalents_in_other_senses(self):
        a = make_sense('apple', 'en', 'A round fruit.')
        self.link(a)
        b = make_sense('manzana', 'es', 'A round fruit.', gloss=['manzana'])
        self.link(b, {'manzana': 'apple'})
        a.refresh_from_db()
        p = concept_payload(a)
        self.assertEqual((p['label'], p['status']), ('apple', Concept.ANCHORED))
        self.assertEqual([(e['word'], e['language_code']) for e in p['equivalents']], [('manzana', 'es')])
        self.assertIsNone(concept_payload(make_sense('solo', 'en', 'x')))

    def test_link_word_links_every_official_sense_and_survives_errors(self):
        a = make_sense('apple', 'en', 'A round fruit.')
        with mock.patch.object(linker, 'embed', fake_embed), mock.patch.object(linker, 'run_judge', judge_picks({})):
            res = linker.link_word(a.word_id)
        self.assertEqual([r['outcome'] for r in res], ['created'])
        with mock.patch.object(linker, 'link_sense', side_effect=RuntimeError('boom')):
            make_sense('hi', 'en', 'A greeting.')
            self.assertEqual(linker.link_word(AIWord.objects.get(value='hi').id), [])  # lỗi bị nuốt


class JudgeTests(SimpleTestCase):
    def cands(self):
        return [{'id': 'A', 'label': 'a', 'pos': 'n', 'description': 'da'}, {'id': 'B', 'label': 'b', 'pos': 'n', 'description': 'db'}]

    def test_links_only_when_all_runs_agree_with_order_reversed(self):
        seen = []

        def ask(prompt):
            seen.append(prompt)
            # lần 1 thứ tự A,B -> chọn 1 (A); lần 2 thứ tự B,A -> chọn 2 (A)
            return {'choice': 1 if len(seen) == 1 else 2, 'near': []}
        with mock.patch.object(judge_module, '_ask', side_effect=ask):
            r = judge_module.judge({'head': 'x', 'pos': 'n', 'definition': 'd'}, self.cands(), runs=2)
        self.assertEqual((r['concept_id'], r['agree']), ('A', True))
        self.assertLess(seen[0].index('1. a'), seen[0].index('2. b'))
        self.assertLess(seen[1].index('1. b'), seen[1].index('2. a'))

    def test_disagreement_returns_no_concept_and_reports_near(self):
        answers = iter([{'choice': 1, 'near': []}, {'choice': 1, 'near': []}])  # lần 2 đảo: 1 = B
        with mock.patch.object(judge_module, '_ask', side_effect=lambda p: next(answers)):
            r = judge_module.judge({'head': 'x', 'pos': 'n', 'definition': 'd'}, self.cands(), runs=2)
        self.assertEqual((r['concept_id'], r['agree']), (None, False))
        self.assertEqual(set(r['near']), {'A', 'B'})

    def test_first_run_none_skips_second_run_but_keeps_near(self):
        calls = []

        def ask(prompt):
            calls.append(prompt)
            return {'choice': 0, 'near': [2]}
        with mock.patch.object(judge_module, '_ask', side_effect=ask):
            r = judge_module.judge({'head': 'x', 'pos': 'n', 'definition': 'd'}, self.cands(), runs=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual((r['concept_id'], r['agree'], r['near']), (None, True, ['B']))

    @override_settings(CONCEPT_JUDGE_EARLY_EXIT=False)
    def test_early_exit_can_be_turned_off(self):
        calls = []
        with mock.patch.object(judge_module, '_ask', side_effect=lambda p: calls.append(p) or {'choice': 0, 'near': []}):
            judge_module.judge({'head': 'x', 'pos': 'n', 'definition': 'd'}, self.cands(), runs=2)
        self.assertEqual(len(calls), 2)

    def test_first_run_choice_still_gets_second_run(self):
        calls = []
        with mock.patch.object(judge_module, '_ask', side_effect=lambda p: calls.append(p) or {'choice': 1, 'near': []}):
            judge_module.judge({'head': 'x', 'pos': 'n', 'definition': 'd'}, self.cands(), runs=2)
        self.assertEqual(len(calls), 2)

    def test_no_candidates_makes_no_call(self):
        with mock.patch.object(judge_module, '_ask') as ask:
            r = judge_module.judge({'head': 'x', 'pos': 'n', 'definition': 'd'}, [])
        ask.assert_not_called()
        self.assertEqual((r['concept_id'], r['agree']), (None, True))


@override_settings(CONCEPT_MIN_COS=0.7)
class PoolCacheTests(TestCase):
    """Ma trận embedding giữ trong RAM, cập nhật theo thay đổi ở DB."""

    def link(self, sense, mapping=None):
        return linker.link_sense(sense, judge_fn=judge_picks(mapping or {}), embed_fn=fake_embed)

    def setUp(self):
        linker._POOL.reset([])

    def test_new_concept_and_attached_vector_show_up_without_full_reload(self):
        self.link(make_sense('apple', 'en', 'A round fruit.'))
        pool = linker._load_pool()
        self.assertEqual(pool.n, 1)
        before = pool.matrix[0].copy()
        with mock.patch.object(linker._POOL, 'reset', wraps=linker._POOL.reset) as reset:
            self.link(make_sense('manzana', 'es', 'A round fruit.', gloss=['manzana']), {'manzana': 'apple'})  # nối: trộn vector
            self.link(make_sense('bank', 'en', 'Money place.'))  # concept mới
            pool = linker._load_pool()
            reset.assert_not_called()
        self.assertEqual(pool.n, 2)
        self.assertFalse(np.allclose(before, pool.matrix[pool.index[str(Concept.objects.get(label='apple').id)]]))  # vector đã trộn với manzana

    def test_merge_or_delete_triggers_full_reload(self):
        self.link(make_sense('apple', 'en', 'A round fruit.'))
        self.link(make_sense('bank', 'en', 'Money place.'))
        self.assertEqual(linker._load_pool().n, 2)
        Concept.objects.filter(label='bank').update(is_deleted=True)
        self.assertEqual(linker._load_pool().n, 1)

    def test_matches_a_fresh_load_after_many_additions(self):
        for name in ('apple', 'bank', 'huge', 'hello', 'sobremesa'):
            self.link(make_sense(name, 'en', f'{name} definition.'))
            linker._load_pool()
        cached = linker._POOL
        fresh = linker._Pool()
        fresh.reset(list(Concept.objects.filter(is_deleted=False).values('id', 'embedding', 'updated_at')))
        self.assertEqual(set(cached.ids), set(fresh.ids))
        for cid in fresh.ids:
            self.assertTrue(np.allclose(cached.matrix[cached.index[cid]], fresh.matrix[fresh.index[cid]]))


class TextTests(SimpleTestCase):
    def test_head_and_gloss_rules(self):
        class S:
            def __init__(self, lang, word, gloss, d='Def.'):
                self.language_code, self.word_value = lang, word
                self.contents = {'definition': {'en': d}, 'translations': {'en': gloss}}
        self.assertEqual(text.head(S('en', 'apple', ['ignored'])), 'apple')
        self.assertEqual(text.head(S('es', 'manzana', ['apple', 'pome'])), 'apple, pome')
        self.assertEqual(text.head(S('ja', '木漏れ日', [])), '木漏れ日')
        self.assertEqual(text.gloss_words(S('es', 'x', ['a very long phrase of words', 'ok'])), ['ok'])
        self.assertEqual(text.embed_text(S('es', 'manzana', ['apple'])), 'apple: Def.')
        self.assertEqual(text.pos_class('Noun (countable)'), 'noun')


class SerializerTests(TestCase):
    def test_sense_serializer_exposes_concept_only_when_linked(self):
        from ai.serializers.AISense import AISenseSerializer
        a = make_sense('apple', 'en', 'A round fruit.')
        self.assertIsNone(AISenseSerializer(a).data['concept'])
        linker.link_sense(a, judge_fn=judge_picks({}), embed_fn=fake_embed)
        b = make_sense('manzana', 'es', 'A round fruit.', gloss=['manzana'])
        linker.link_sense(b, judge_fn=judge_picks({'manzana': 'apple'}), embed_fn=fake_embed)
        a.refresh_from_db()
        data = AISenseSerializer(a).data['concept']
        self.assertEqual((data['label'], [e['word'] for e in data['equivalents']]), ('apple', ['manzana']))
