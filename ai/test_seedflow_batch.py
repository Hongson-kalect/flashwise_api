"""Luồng batch của seed_flow: hook ghi / phát lại Gemini (utils/ai/gemini_batch.py), giai đoạn enrich và brief ảnh chạy qua Gemini Batch (Gemini giả)."""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from ai.seedflow import flow
from utils.ai import gemini_batch, translate


def quiet_usage(case):
    """Replayer ghi token vào bộ đếm ai_usage thật trên Redis: không để test làm lệch số đo của run đang chạy."""
    patcher = mock.patch.object(gemini_batch.usage, '_add')
    patcher.start()
    case.addCleanup(patcher.stop)


def done_response(items):
    """Phản hồi GET job Gemini khi xong: items = {khoá: dict JSON model trả}."""
    inlined = [{'metadata': {'key': k},
                'response': {'candidates': [{'content': {'parts': [{'text': json.dumps(v)}]}}],
                             'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 5}}} for k, v in items.items()]
    return {'done': True, 'metadata': {'state': 'BATCH_STATE_SUCCEEDED'}, 'response': {'inlinedResponses': {'inlinedResponses': inlined}}}


class HookTests(SimpleTestCase):
    def setUp(self):
        quiet_usage(self)

    def test_without_hook_intercept_returns_none(self):
        self.assertIsNone(asyncio.run(gemini_batch.intercept('translate', 'm', 'p', {})))

    def test_recorder_collects_and_returns_empty_placeholder(self):
        rec = gemini_batch.Recorder()
        with gemini_batch.use(rec):
            data = asyncio.run(gemini_batch.intercept('translate', 'gemini-3.1-flash-lite', 'hello', {'type': 'OBJECT'}))
            asyncio.run(gemini_batch.intercept('translate', 'gemini-3.1-flash-lite', 'hello', {'type': 'OBJECT'}))  # trùng: chỉ một request
        self.assertEqual(data, {})
        self.assertEqual(len(rec.requests), 1)
        entry = next(iter(rec.requests.values()))
        self.assertEqual(entry['request']['generationConfig']['thinkingConfig'], {'thinkingLevel': 'minimal'})
        self.assertEqual(entry['request']['contents'][0]['parts'][0]['text'], 'hello')

    def test_thinking_config_by_model_family(self):
        self.assertEqual(gemini_batch.thinking_config('gemini-2.5-flash-lite'), {'thinkingBudget': 0})
        self.assertEqual(gemini_batch.thinking_config('gemini-3.1-flash-lite'), {'thinkingLevel': 'minimal'})

    def test_replayer_returns_result_or_none_for_missing_and_broken(self):
        key = gemini_batch.request_key('m', 'p', {'a': 1}, None)
        rp = gemini_batch.Replayer({key: {'text': '{"x": 1}', 'in': 3, 'out': 2},
                                    gemini_batch.request_key('m', 'bad', {}, None): {'text': 'not json'},
                                    gemini_batch.request_key('m', 'list', {}, None): {'text': '[1]'}})
        with gemini_batch.use(rp):
            self.assertEqual(asyncio.run(gemini_batch.intercept('translate', 'm', 'p', {'a': 1})), {'x': 1})
            self.assertIsNone(asyncio.run(gemini_batch.intercept('translate', 'm', 'other', {})))
            self.assertIsNone(asyncio.run(gemini_batch.intercept('translate', 'm', 'bad', {})))
            self.assertIsNone(asyncio.run(gemini_batch.intercept('translate', 'm', 'list', {})))
        self.assertEqual((rp.hits, rp.misses), (1, 3))

    def test_key_depends_on_model_prompt_schema_and_system(self):
        base = gemini_batch.request_key('m', 'p', {'a': 1}, 's')
        self.assertEqual(base, gemini_batch.request_key('m', 'p', {'a': 1}, 's'))
        for other in (('m2', 'p', {'a': 1}, 's'), ('m', 'p2', {'a': 1}, 's'), ('m', 'p', {'a': 2}, 's'), ('m', 'p', {'a': 1}, 't')):
            self.assertNotEqual(base, gemini_batch.request_key(*other))

    def test_submit_splits_jobs_and_poll_reads_results_by_key(self):
        rec = gemini_batch.Recorder()
        for i in range(3):
            rec.handle('translate', 'gemini-3.1-flash-lite', f'p{i}', {}, None, 100)
        posted = []
        with mock.patch.object(gemini_batch, 'MAX_PER_JOB', 2), mock.patch.object(
                gemini_batch, '_http', side_effect=lambda method, path, body=None, attempts=4: posted.append((path, body)) or {'name': f'batches/{len(posted)}'}):
            jobs = gemini_batch.submit(rec.requests, 'r1_enrich')
        self.assertEqual([len(j['keys']) for j in jobs], [2, 1])
        self.assertTrue(all(p.startswith('models/gemini-3.1-flash-lite:batchGenerateContent') for p, _ in posted))
        keys = jobs[0]['keys']
        with mock.patch.object(gemini_batch, '_http', return_value=done_response({keys[0]: {'a': 1}, keys[1]: {'a': 2}})):
            state, results = gemini_batch.poll(jobs[0])
        self.assertEqual(state, 'done')
        self.assertEqual(json.loads(results[keys[1]]['text']), {'a': 2})
        self.assertEqual((results[keys[0]]['in'], results[keys[0]]['out']), (10, 5))

    def test_poll_running_and_dead(self):
        job = {'name': 'batches/1', 'keys': ['k']}
        with mock.patch.object(gemini_batch, '_http', return_value={'metadata': {'state': 'BATCH_STATE_RUNNING'}}):
            self.assertEqual(gemini_batch.poll(job), ('running', {}))
        with mock.patch.object(gemini_batch, '_http', return_value={'metadata': {'state': 'BATCH_STATE_EXPIRED'}}):
            self.assertEqual(gemini_batch.poll(job), ('dead', {}))


def fake_client():
    class Aio:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return mock.Mock(aio=Aio())


class TranslateHookTests(SimpleTestCase):
    """process_translation_chunk dưới hook: lượt ghi không đổi gì, lượt phát lại áp đúng kết quả batch."""

    def setUp(self):
        self.sense = {'id': 'sid', 'word_value': 'cat', 'pos': 'noun', 'contents': {'definition': {'en': {'value': 'a feline'}}, 'examples': []}}
        self.chunk = {'s1': {'pos': 'noun', 'definition': 'a feline'}}
        quiet_usage(self)
        patcher = mock.patch.object(translate.genai, 'Client', return_value=fake_client())
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self):
        return asyncio.run(translate.process_translation_chunk.__wrapped__(self.chunk, 'en', 'vi', ['vi'], {'s1': 'sid'}, {'sid': self.sense}, None))

    def test_record_then_replay(self):
        rec = gemini_batch.Recorder()
        with gemini_batch.use(rec):
            self.assertEqual(self.call(), {})
        self.assertEqual(len(rec.requests), 1)
        key, entry = next(iter(rec.requests.items()))
        self.assertIn("word 'cat'", entry['request']['contents'][0]['parts'][0]['text'])
        answer = {'s1': {'definition': {'vi': 'con mèo'}, 'translations': {'vi': ['mèo']}}}
        rp = gemini_batch.Replayer({key: {'text': json.dumps(answer), 'in': 1, 'out': 1}})
        with gemini_batch.use(rp):
            out = self.call()
        self.assertEqual(out['sid']['definition']['vi'], {'value': 'con mèo'})
        self.assertEqual(out['sid']['translations']['vi'], ['mèo'])
        self.assertEqual(rp.hits, 1)


class BatchFlowBase(TestCase):
    def setUp(self):
        quiet_usage(self)
        get_user_model().objects.create_superuser('root', password='x')
        self.tmp = tempfile.mkdtemp()
        mock.patch.object(flow, 'RUNS_DIR', Path(self.tmp)).start()
        self.addCleanup(mock.patch.stopall)
        step = Path(self.tmp) / 'step.jsonl'
        step.write_text(json.dumps({'w': 'zzcat', 'rank': 1}) + '\n', encoding='utf-8')
        self.run_ = flow.Run.create('t', step, 'en', ['en', 'vi'], 'batch')


class EnrichBatchTests(BatchFlowBase):
    def setUp(self):
        super().setUp()
        self.run_.state['words']['zzcat'].update({'id': 'wid', 'status': 'refined'})
        self.run_.senses['zzcat'] = [{'id': 'sid', 'pos': 'noun', 'contents': {'definition': {'en': {'value': 'a feline'}}, 'examples': []}}]

    def answers(self, recorded):
        out = {}
        for key, entry in recorded.items():
            out[key] = ({'s1': {'tags': ['pet'], 'image_keywords': ['cat']}} if entry['label'] == 'metadata'
                        else {'s1': {'definition': {'vi': 'con mèo'}, 'translations': {'vi': ['mèo']}}})
        return out

    def test_submit_wait_then_apply_without_calling_gemini_online(self):
        submitted = {}

        def fake_submit(recorded, name):
            submitted.update(recorded)
            return [{'name': 'batches/1', 'keys': list(recorded), 'model': 'm'}]

        online = mock.AsyncMock(side_effect=AssertionError('không được gọi Gemini online'))
        client = mock.Mock(aio=mock.MagicMock())
        client.aio.__aenter__ = mock.AsyncMock(return_value=mock.Mock(models=mock.Mock(generate_content=online)))
        client.aio.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch('utils.ai.metadata.socket_message', new=mock.AsyncMock()), mock.patch.object(gemini_batch, 'submit', side_effect=fake_submit) as sub, \
                mock.patch.object(translate.genai, 'Client', return_value=client), mock.patch('utils.ai.metadata.genai.Client', return_value=client):
            self.assertFalse(flow.stage_enrich(self.run_))  # ghi + gửi
            self.assertEqual(sub.call_count, 1)
            self.assertEqual(sorted(e['label'] for e in submitted.values()), ['metadata', 'translate'])
            self.assertEqual(self.run_.state['words']['zzcat']['status'], 'refined')

            with mock.patch.object(gemini_batch, 'poll', return_value=('running', {})):
                self.assertFalse(flow.stage_enrich(self.run_))  # chờ, không gửi lần hai
            self.assertEqual(sub.call_count, 1)

            results = {k: {'text': json.dumps(v), 'in': 5, 'out': 5} for k, v in self.answers(submitted).items()}
            with mock.patch.object(gemini_batch, 'poll', return_value=('done', results)):
                self.assertTrue(flow.stage_enrich(self.run_))  # phát lại
        sense = self.run_.senses['zzcat'][0]
        self.assertEqual(sense['contents']['definition']['vi'], {'value': 'con mèo'})
        self.assertEqual(sense['metadata']['tags'], ['pet'])
        self.assertEqual(self.run_.state['words']['zzcat']['status'], 'enriched')
        self.assertEqual(self.run_.state['enrich_batch'], {'từ batch': 2, 'gọi online bù': 0})
        self.assertTrue((self.run_.dir / 'gemini_enrich.json').exists())
        online.assert_not_called()

    def test_online_mode_never_touches_batch(self):
        self.run_.state['mode'] = 'online'
        with mock.patch.object(flow, '_gemini_phase', side_effect=AssertionError), mock.patch.object(flow, '_run_enrich', return_value=[('zzcat', [])]):
            self.assertTrue(flow.stage_enrich(self.run_))
        self.assertEqual(self.run_.state['words']['zzcat']['status'], 'enriched')

    def test_dead_job_falls_back_to_online_and_records_issue(self):
        self.run_.state.setdefault('gbatches', {})['enrich'] = {'jobs': [{'name': 'batches/1', 'keys': ['k']}], 'requests': 1}
        with mock.patch.object(gemini_batch, 'poll', return_value=('dead', {})), mock.patch.object(flow, '_run_enrich', return_value=[('zzcat', [])]):
            self.assertTrue(flow.stage_enrich(self.run_))
        self.assertTrue(any(i['stage'] == 'enrich' for i in self.run_.state['issues']))


class BriefBatchTests(BatchFlowBase):
    def setUp(self):
        super().setUp()
        self.meta = mock.Mock(image_metadata={})
        sense = mock.Mock(metadata=self.meta)
        item = {'key': 'k1', 'pos': 'noun', 'definition': 'a feline', 'example': ''}
        self.chunks = [('cat', [item], 'Word: cat\nSenses:\n1. (noun) a feline')]
        mock.patch.object(flow, '_brief_chunks', return_value=(self.chunks, {'k1': sense})).start()

    @override_settings(IMAGE_BRIEF_PROVIDER='gemini')
    def test_prefill_submits_waits_and_writes_metadata(self):
        from utils.image_gen import brief
        submitted = {}
        with mock.patch.object(gemini_batch, 'submit', side_effect=lambda rec, name: submitted.update(rec) or [{'name': 'b/1', 'keys': list(rec), 'model': 'm'}]):
            self.assertFalse(flow._prefill_briefs(self.run_))
        self.assertEqual(len(submitted), 1)
        key = next(iter(submitted))
        answer = {'briefs': [{'n': 1, 'image_kind': 'object', 'image_subject': 'cat', 'image_prompt': 'A simple drawing of a cat sitting on a mat.'}]}
        results = {key: {'text': json.dumps(answer), 'in': 5, 'out': 5}}
        with mock.patch.object(gemini_batch, 'poll', return_value=('done', results)):
            self.assertTrue(flow._prefill_briefs(self.run_))
        self.assertEqual(self.meta.image_metadata['kind'], 'object')
        self.assertEqual(self.meta.image_metadata['subject'], 'cat')
        self.assertEqual(self.meta.image_metadata['source'], brief.provider())
        self.meta.save.assert_called_once()
        self.assertEqual(self.run_.state['briefs_batch']['brief batch viết'], 1)

    @override_settings(IMAGE_BRIEF_PROVIDER='claude')
    def test_claude_provider_skips_gemini_batch(self):
        with mock.patch.object(gemini_batch, 'submit', side_effect=AssertionError):
            self.assertTrue(flow._prefill_briefs(self.run_))

    @override_settings(IMAGE_GEN_ENABLED=True)
    def test_images_stage_waits_for_brief_batch_and_online_mode_skips_it(self):
        with mock.patch.object(flow, '_prefill_briefs', return_value=False):
            self.assertFalse(flow.stage_images(self.run_))
        self.run_.state['mode'] = 'online'
        with mock.patch.object(flow, '_prefill_briefs', side_effect=AssertionError), mock.patch('utils.image_gen.service.prepare_and_enqueue', new=mock.AsyncMock()):
            self.assertTrue(flow.stage_images(self.run_))
