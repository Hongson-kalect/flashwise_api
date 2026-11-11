"""SEED_ENRICH_PROVIDER=claude: dịch / metadata / brief ở luồng batch chạy bằng Claude Message Batches (Haiku) thay Gemini Batch."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from ai.seedflow import flow, llm
from utils.ai import gemini_batch
from utils.ai.schema import render_enhanced_schema


def outcome(text, tin=100, tout=50, stop='end_turn', ok=True):
    return {'ok': ok, 'text': text, 'stop_reason': stop, 'tokens_in': tin, 'tokens_out': tout, 'error': ''}


@override_settings(SEED_ENRICH_PROVIDER='claude')
class ClaudeEnrichPhaseTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser('root', password='x')
        self.tmp = tempfile.mkdtemp()
        mock.patch.object(flow, 'RUNS_DIR', Path(self.tmp)).start()
        mock.patch.object(gemini_batch.usage, '_add').start()
        self.addCleanup(mock.patch.stopall)
        step = Path(self.tmp) / 'step.jsonl'
        step.write_text(json.dumps({'w': 'zzcat', 'rank': 1}) + '\n', encoding='utf-8')
        self.run_ = flow.Run.create('t', step, 'en', ['en', 'vi'], 'batch')
        self.schema = {'type': 'OBJECT', 'properties': {'a': {'type': 'ARRAY', 'items': {'type': 'STRING'}, 'maxItems': 5}}}

    def record(self, recorder):
        recorder.handle('translate', 'gemini-3.1-flash-lite', 'prompt T', self.schema, None, 8192)
        recorder.handle('metadata', 'gemini-2.5-flash-lite', 'prompt M', render_enhanced_schema(['s1']), 'sys M', 3000)

    def test_provider_switch(self):
        self.assertEqual(flow.enrich_provider(), 'claude')
        with override_settings(SEED_ENRICH_PROVIDER='gemini'):
            self.assertEqual(flow.enrich_provider(), 'gemini')
        with override_settings(SEED_ENRICH_PROVIDER='lạ'):
            self.assertEqual(flow.enrich_provider(), 'gemini')

    def test_requests_use_haiku_with_json_schema_and_no_thinking(self):
        recorder = gemini_batch.Recorder()
        self.record(recorder)
        requests = {r['custom_id']: r['params'] for r in flow._claude_requests(recorder.requests)}
        self.assertEqual(len(requests), 2)
        for key, params in requests.items():
            self.assertEqual(params['model'], 'claude-haiku-5-5')
            self.assertEqual(params['thinking'], {'type': 'disabled'})
        by_prompt = {p['messages'][0]['content']: p for p in requests.values()}
        self.assertEqual(by_prompt['prompt T']['max_tokens'], 8192)
        translate = by_prompt['prompt T']['output_config']['format']['schema']
        self.assertEqual(translate['properties']['a']['type'], 'array')
        self.assertNotIn('maxItems', translate['properties']['a'])  # Claude không nhận maxItems: cắt ở code (dedupe)
        self.assertIn('senses', by_prompt['prompt M']['output_config']['format']['schema']['properties'])
        self.assertNotIn('system', by_prompt['prompt T'])
        self.assertEqual((by_prompt['prompt M']['system'], by_prompt['prompt M']['max_tokens']), ('sys M', 3000))

    def test_phase_submits_waits_then_replays_with_per_label_usage(self):
        keys = {}

        def rec(recorder):
            self.record(recorder)
            keys.update({e['label']: k for k, e in recorder.requests.items()})

        with mock.patch.object(llm, 'submit_batch', return_value='msgbatch_1') as submit, \
                mock.patch.object(llm, 'batch_status', return_value=('in_progress', {'processing': 2})):
            self.assertIsNone(flow._gemini_phase(self.run_, 'enrich', rec))
            self.assertEqual(submit.call_count, 1)
            self.assertIsNone(flow._gemini_phase(self.run_, 'enrich', rec))  # chưa xong: không gửi lần hai
            self.assertEqual(submit.call_count, 1)
        outcomes = {keys['translate']: outcome('{"a": ["x"]}', 100, 50), keys['metadata']: outcome('{"a": ["y"]}', 300, 80)}
        with mock.patch.object(llm, 'batch_status', return_value=('ended', {'succeeded': 2})), \
                mock.patch.object(llm, 'batch_results', return_value=outcomes):
            replayer = flow._gemini_phase(self.run_, 'enrich', rec)
        self.assertIsInstance(replayer, gemini_batch.Replayer)
        self.assertEqual(replayer.handle('translate', 'gemini-3.1-flash-lite', 'prompt T', self.schema, None, 8192), {'a': ['x']})
        usage = self.run_.state['usage']
        self.assertEqual((usage['enrich_translate']['in'], usage['enrich_translate']['out']), (100, 50))
        self.assertEqual((usage['enrich_metadata']['in'], usage['enrich_metadata']['out']), (300, 80))
        self.assertAlmostEqual(usage['enrich_translate']['usd'], llm.cost(100, 50, batch=True), delta=1e-5)  # usd làm tròn 5 chữ số
        self.assertTrue((self.run_.dir / 'gemini_enrich.json').exists())  # chạy lại không gửi batch nữa

    def test_truncated_or_failed_requests_are_misses_for_online_fallback(self):
        info = {'labels': {'k1': 'metadata', 'k2': 'metadata', 'k3': 'brief'}}
        outcomes = {'k1': outcome('{"a": [', stop='max_tokens'), 'k2': llm.failed('errored'), 'k3': outcome('{"b": 1}')}
        results = flow._claude_results(self.run_, info, outcomes)
        self.assertEqual(list(results), ['k3'])
        self.assertEqual(results['k3']['provider'], 'claude')
        self.assertEqual(self.run_.state['usage']['enrich_metadata']['requests'], 2)  # vẫn tính token đã trả tiền

    def test_replayer_counts_usage_under_the_item_provider(self):
        replayer = gemini_batch.Replayer({'k': {'text': '{"a": 1}', 'in': 5, 'out': 6, 'provider': 'claude'}})
        with mock.patch.object(gemini_batch, 'request_key', return_value='k'):
            replayer.handle('translate', 'm', 'c', {}, None, 100)
        gemini_batch.usage._add.assert_called_with('translate_batch', 'claude', 5, 6)


class MetadataArrayTests(TestCase):
    def test_schema_is_a_fixed_size_array_whatever_the_sense_count(self):
        from utils.ai.schema import render_enhanced_schema
        one = flow.metadata_array_schema(render_enhanced_schema(['a']))
        nine = flow.metadata_array_schema(render_enhanced_schema([f's{i}' for i in range(9)]))
        self.assertEqual(one, nine)
        item = nine['properties']['senses']['items']
        self.assertEqual(item['required'], ['id', 'tags', 'image_keywords'])
        self.assertEqual(set(item['properties']), {'id', 'collocations', 'idioms', 'relateds', 'synonyms', 'antonyms', 'tags', 'image_keywords'})

    def test_array_result_converts_back_to_dict_by_sense_id(self):
        text = json.dumps({'senses': [{'id': 'u1', 'tags': ['a'], 'image_keywords': ['x']}, {'id': 'u2', 'tags': ['b'], 'image_keywords': []}]})
        self.assertEqual(json.loads(flow.metadata_array_to_dict(text)),
                         {'u1': {'tags': ['a'], 'image_keywords': ['x']}, 'u2': {'tags': ['b'], 'image_keywords': []}})
        self.assertIsNone(flow.metadata_array_to_dict('{"senses": [{"tags": []}]}'))  # thiếu id
        self.assertIsNone(flow.metadata_array_to_dict('không phải json'))

    def test_only_metadata_requests_use_the_array_schema(self):
        recorder = gemini_batch.Recorder()
        from utils.ai.schema import render_enhanced_schema
        schema = render_enhanced_schema(['a', 'b'])
        recorder.handle('metadata', 'm', 'p1', schema, None, 100)
        recorder.handle('translate', 'm', 'p2', schema, None, 100)
        out = {r['params']['messages'][0]['content']: r['params']['output_config']['format']['schema'] for r in flow._claude_requests(recorder.requests)}
        self.assertIn('senses', out['p1']['properties'])
        self.assertIn('a', out['p2']['properties'])

    def test_metadata_results_are_converted_when_replayed(self):
        run = mock.Mock()
        info = {'labels': {'k1': 'metadata', 'k2': 'metadata'}}
        good = json.dumps({'senses': [{'id': 'u1', 'tags': ['a'], 'image_keywords': ['x']}]})
        results = flow._claude_results(run, info, {'k1': outcome(good), 'k2': outcome('{"senses": [{"tags": []}]}')})
        self.assertEqual(json.loads(results['k1']['text']), {'u1': {'tags': ['a'], 'image_keywords': ['x']}})
        self.assertNotIn('k2', results)  # id thiếu: coi như không có kết quả, chạy online bù
