"""Dữ liệu từ do agent ngoài viết trọn (ai/seedflow/external.py): xuất, kiểm schema, nhập và lưu như kết quả của luồng thường."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from ai.models import AISense
from ai.seedflow import external, flow


def entry(**over):
    langs = ['en', 'vi', 'zh']
    sense = {'pos': 'noun', 'definition': {l: f'def {l}' for l in langs}, 'usage': {'en': '', 'vi': '', 'zh': ''},
             'examples': [{l: f'The cat sleeps {l}.' for l in langs}], 'translations': {'vi': ['mèo'], 'zh': ['猫']},
             'is_offensive': False, 'level': 'A1', 'register': ['neutral'], 'ipas': [{'value': '/kæt/', 'label': 'US'}],
             'metadata': {'tags': ['pet'], 'image_keywords': ['cat'], 'collocations': ['pet cat'], 'idioms': [], 'relateds': [], 'synonyms': [], 'antonyms': []},
             'image': {'kind': 'object', 'subject': 'cat', 'prompt': 'A small furry cat sitting upright, with pointed ears and a long tail.'}}
    sense.update(over)
    return {'word': 'zzcat', 'should_be_saved': True, 'senses': [sense]}


class ValidateTests(SimpleTestCase):
    def test_schema_accepts_good_and_reports_missing_and_wrong_types(self):
        schema = external.output_schema('en', ['vi', 'zh'])
        self.assertEqual(external.validate(entry(), schema), [])
        bad = entry()
        del bad['senses'][0]['translations']['zh']
        bad['senses'][0]['is_offensive'] = 'no'
        errors = external.validate(bad, schema)
        self.assertTrue(any('translations.zh' in e for e in errors))
        self.assertTrue(any('is_offensive' in e for e in errors))


class ImportTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser('root', password='x')
        self.tmp = tempfile.mkdtemp()
        mock.patch.object(flow, 'RUNS_DIR', Path(self.tmp)).start()
        self.addCleanup(mock.patch.stopall)
        step = Path(self.tmp) / 'step.jsonl'
        step.write_text(''.join(json.dumps(r) + '\n' for r in [{'w': 'zzcat', 'rank': 1}, {'w': 'zzbad', 'rank': 2}, {'w': 'zznone', 'rank': 3}]), encoding='utf-8')
        self.run_ = flow.Run.create('t', step, 'en', ['en', 'vi', 'zh'], 'online')
        flow.stage_prepare(self.run_)
        self.dir = Path(self.tmp) / 'ext'

    def write(self, word, data):
        self.dir.mkdir(exist_ok=True)
        (self.dir / f'{word}.out.json').write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')

    def test_export_writes_instructions_and_one_file_per_new_word(self):
        self.assertEqual(external.export_words(self.run_, self.dir), 3)
        text = (self.dir / 'INSTRUCTIONS.md').read_text(encoding='utf-8')
        self.assertIn('zh (', text)
        self.assertIn('image_subject', text)
        self.assertEqual(json.loads((self.dir / 'zzcat.in.json').read_text(encoding='utf-8'))['word'], 'zzcat')

    def test_import_builds_enriched_senses_without_touching_the_db(self):
        self.write('zzcat', entry())
        self.write('zzbad', '{not json')
        report = external.import_words(self.run_, self.dir, flow.sanitize_senses, flow.missing_enrichment)
        self.assertEqual(report['imported'], ['zzcat'])
        self.assertEqual([w for w, _ in report['invalid']], ['zzbad'])
        self.assertEqual(report['missing'], ['zznone'])
        self.assertEqual(self.run_.state['words']['zzcat']['status'], 'enriched')
        self.assertEqual(self.run_.state['words']['zzbad']['status'], 'new')
        sense = self.run_.senses['zzcat'][0]
        self.assertEqual(sense['contents']['definition']['vi'], {'value': 'def vi'})
        self.assertEqual(sense['contents']['examples'][0]['zh'], {'value': 'The cat sleeps zh.'})
        self.assertNotIn('vi', sense['contents']['usage'])  # usage rỗng thì không có bản dịch
        self.assertEqual(sense['contents']['translations'], {'vi': ['mèo'], 'zh': ['猫']})
        self.assertEqual(sense['metadata']['advanced'], {'collocations': ['pet cat']})
        self.assertEqual(sense['metadata']['image_metadata']['kind'], 'object')
        self.assertEqual(flow.missing_enrichment([sense], 'en', ['en', 'vi', 'zh']), [])
        self.assertFalse(AISense.objects.filter(word_value='zzcat').exists())

    def test_rejected_and_unusable_brief(self):
        self.write('zzcat', {**entry(), 'should_be_saved': False, 'senses': []})
        self.write('zzbad', entry(image={'kind': 'object', 'subject': 'cat', 'prompt': 'x'}))
        report = external.import_words(self.run_, self.dir, flow.sanitize_senses, flow.missing_enrichment)
        self.assertEqual(report['rejected'], ['zzcat'])
        self.assertEqual(self.run_.senses['zzbad'][0]['metadata']['image_metadata'], {})  # brief hỏng: luồng viết lại sau

    def test_imported_words_go_through_save_like_normal_ones(self):
        self.write('zzcat', entry())
        external.import_words(self.run_, self.dir, flow.sanitize_senses, flow.missing_enrichment)
        flow.stage_save(self.run_)
        saved = AISense.objects.get(word_value='zzcat')
        self.assertEqual(saved.contents['definition']['zh']['value'], 'def zh')
        self.assertEqual(saved.metadata.image_metadata['subject'], 'cat')
        self.assertEqual(saved.quality_status, 'refined')


class AgentVerifyConceptTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser('root', password='x')
        self.tmp = tempfile.mkdtemp()
        mock.patch.object(flow, 'RUNS_DIR', Path(self.tmp)).start()
        self.addCleanup(mock.patch.stopall)
        step = Path(self.tmp) / 'step.jsonl'
        step.write_text(json.dumps({'w': 'zzcat', 'rank': 1}) + '\n', encoding='utf-8')
        self.run_ = flow.Run.create('t', step, 'en', ['en', 'vi'], 'online')
        self.dir = Path(self.tmp) / 'ext'
        self.dir.mkdir()

    def verify_spec(self):
        (self.dir / 'zzcat.verify.in.json').write_text(json.dumps({'word': 'zzcat', 'custom_id': 'c1', 'system': 's', 'prompt': 'p', 'schema': {}}), encoding='utf-8')

    def test_import_verify_validates_against_the_verify_schema(self):
        self.verify_spec()
        self.assertEqual(external.import_verify(self.run_, self.dir)['missing'], ['zzcat'])
        good = {'checks': [{'sense': 0, 'lang': 'vi', 'back': 'x', 'verdict': 'ok', 'score': 90}], 'fixes': []}
        (self.dir / 'zzcat.verify.out.json').write_text(json.dumps(good), encoding='utf-8')
        report = external.import_verify(self.run_, self.dir)
        self.assertEqual(report['ok'], 1)
        saved = json.loads((self.run_.dir / 'verify_external.json').read_text(encoding='utf-8'))
        self.assertEqual(json.loads(saved['c1']['text']), good)
        self.assertTrue(saved['c1']['ok'])
        bad = {'checks': [{'sense': 0, 'lang': 'vi', 'back': 'x', 'verdict': 'maybe', 'score': 90}], 'fixes': []}
        (self.dir / 'zzcat.verify.out.json').write_text(json.dumps(bad), encoding='utf-8')
        report = external.import_verify(self.run_, self.dir)
        self.assertEqual((report['ok'], len(report['invalid'])), (0, 1))

    def test_stage_verify_waits_for_agent_then_applies_without_api(self):
        meta = {'c1': ('zzcat', [], ['en', 'vi'])}
        requests = [{'custom_id': 'c1', 'params': {}}]
        self.run_.state['words']['zzcat'].update({'id': 'wid', 'status': 'saved'})
        self.run_.state['agent_verify'] = True
        with mock.patch('ai.seedflow.verify_db.build_requests', return_value=(requests, meta)), \
                mock.patch.object(flow, '_batch_or_online', side_effect=AssertionError('không được gọi API')), \
                mock.patch('ai.seedflow.verify_db.apply_results', return_value={'ok': 1}) as apply:
            self.assertFalse(flow.stage_verify(self.run_))
            (self.run_.dir / 'verify_external.json').write_text(json.dumps({'c1': {'ok': True, 'text': '{}'}}), encoding='utf-8')
            self.assertTrue(flow.stage_verify(self.run_))
        self.assertEqual(apply.call_args.kwargs['model'], external.MODEL_TAG)

    def test_import_concept_maps_choices_to_concept_ids_and_rejects_bad_answers(self):
        task = {'id': 's1', 'key': 'cat|a feline', 'candidate_ids': ['c-a', 'c-b', 'c-c'], 'prompt': 'p', 'candidates': 3}
        (self.dir / 'zzcat.concept.in.json').write_text(json.dumps({'word': 'zzcat', 'system': 's', 'tasks': [task]}), encoding='utf-8')
        (self.dir / 'zzcat.concept.out.json').write_text(json.dumps({'s1': {'choice': 2, 'near': [1, 2, 3]}}), encoding='utf-8')
        self.assertEqual(external.import_concept(self.run_, self.dir)['ok'], 1)
        decision = json.loads((self.run_.dir / 'concept_external.json').read_text(encoding='utf-8'))['cat|a feline']
        self.assertEqual((decision['concept_id'], decision['near']), ('c-b', ['c-a', 'c-c']))
        (self.dir / 'zzcat.concept.out.json').write_text(json.dumps({'s1': {'choice': 9, 'near': []}}), encoding='utf-8')
        self.assertEqual(len(external.import_concept(self.run_, self.dir)['invalid']), 1)

    def test_concept_judge_uses_agent_when_candidates_are_a_subset_else_falls_back(self):
        decisions = {'cat|a feline': {'concept_id': 'c-b', 'near': ['c-a'], 'cands': ['c-a', 'c-b', 'c-c']}}
        fallback = mock.Mock(return_value={'concept_id': None, 'near': [], 'runs': [], 'agree': True})
        judge, counters = external.concept_judge(decisions, fallback)
        info = {'head': 'cat', 'pos': 'noun', 'definition': 'a feline'}
        out = judge(info, [{'id': 'c-a'}, {'id': 'c-b'}])  # c-c đã bị loại (cùng từ): vẫn dùng quyết định
        self.assertEqual((out['concept_id'], out['near']), ('c-b', ['c-a']))
        self.assertEqual(judge(info, [{'id': 'c-a'}])['concept_id'], None)  # concept được chọn không còn trong danh sách
        fallback.assert_not_called()
        judge(info, [{'id': 'c-a'}, {'id': 'c-new'}])  # ứng viên mới (vừa tạo trong lô): hỏi judge thường
        judge({'head': 'dog', 'pos': 'noun', 'definition': 'x'}, [{'id': 'c-a'}])  # không có quyết định
        self.assertEqual(fallback.call_count, 2)
        self.assertEqual(counters, {'agent': 2, 'fallback': 2})
