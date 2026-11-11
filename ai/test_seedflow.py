"""Luồng seed theo step (ai/seedflow): chia step, lọc từ nhiều nghĩa, các giai đoạn draft / refine với Claude giả."""
import json
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from ai.models import AISense, AIWord
from ai.seedflow import flow, llm
from ai.seedflow.steps import make_step_file, read_source, read_step


def write_source(rows):
    f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False, encoding='utf-8')
    f.write(''.join(json.dumps(r) + '\n' for r in rows))
    f.close()
    return f.name


def row(w, rank, n=5):
    return {'w': w, 'rank': rank, 'n_senses': n, 'entries': [{'pos': 'noun', 'glosses': [f'{w} gloss'], 'n': n}]}


class StepTests(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.none = lambda words, lang: set()

    def test_multi_sense_words_go_to_their_own_file(self):
        src = write_source([row('cat', 1), row('run', 2, 117), row('dog', 3), row('take', 4, 102), row('sun', 5)])
        info = make_step_file(src, self.dir, 2, existing_fn=self.none)
        self.assertEqual([r['w'] for r in read_step(Path(self.dir) / info['file'])], ['cat', 'dog'])
        self.assertEqual(info['multi_sense_moved'], 1)  # chỉ 'run' nằm trước 'dog'
        info2 = make_step_file(src, self.dir, 2, existing_fn=self.none)
        self.assertEqual([r['w'] for r in read_step(Path(self.dir) / info2['file'])], ['sun'])
        multi = [json.loads(l)['w'] for l in (Path(self.dir) / 'multi_sense.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(multi, ['run', 'take'])

    def test_steps_are_sequential_and_idempotent(self):
        src = write_source([row(f'w{i}', i) for i in range(1, 8)])
        first = make_step_file(src, self.dir, 3, existing_fn=self.none)
        self.assertEqual((first['step'], first['from_rank'], first['to_rank']), (1, 1, 3))
        second = make_step_file(src, self.dir, 3, existing_fn=self.none)
        self.assertEqual((second['step'], second['from_rank'], second['to_rank']), (2, 4, 6))
        self.assertEqual(make_step_file(src, self.dir, 3, step=1, existing_fn=self.none), first)  # đã có: trả bản cũ
        with self.assertRaises(ValueError):
            make_step_file(src, self.dir, 3, step=5, existing_fn=self.none)

    def test_skips_existing_invalid_and_duplicates_and_ends(self):
        src = write_source([row('cat', 1), row('cat', 2), row('!!!', 3), row('dog', 4)])
        info = make_step_file(src, self.dir, 5, existing_fn=lambda words, lang: {'dog'})
        self.assertEqual([r['w'] for r in read_step(Path(self.dir) / info['file'])], ['cat'])
        self.assertEqual(info['skipped'], {'existing': 1, 'invalid': 1, 'duplicate': 1})
        with self.assertRaises(ValueError):
            make_step_file(src, self.dir, 5, existing_fn=self.none)  # hết nguồn

    def test_reads_plain_and_tsv_sources_and_strips_sense_counts(self):
        txt = tempfile.NamedTemporaryFile('w', suffix='.tsv', delete=False, encoding='utf-8')
        txt.write('rank\tword\tpos\n1\tcat\tnoun\n2\tdog\tnoun\n')
        txt.close()
        self.assertEqual([r['w'] for r in read_source(txt.name)], ['cat', 'dog'])
        entry = read_source(write_source([row('cat', 1)]))[0]['reference'][0]
        self.assertNotIn('n', entry)


def draft_json(senses, ok=True):
    return json.dumps({'metadata': {'should_be_saved': ok}, 'senses': senses})


SENSE = {'pos': 'noun', 'definition': {'value': 'A small furry pet.'}, 'usage': {'value': ''}, 'examples': [{'value': 'The cat sleeps.'}],
         'is_offensive': False, 'level': 'A1', 'register': ['neutral'], 'ipas': [{'value': '/kæt/', 'label': 'US'}]}


def outcome(text, stop='end_turn'):
    return {'ok': bool(text), 'text': text, 'stop_reason': stop, 'tokens_in': 1000, 'tokens_out': 500, 'error': ''}


@override_settings(AI_REFINE_ENABLED=True)
class FlowStageTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser('root', password='x')
        self.tmp = tempfile.mkdtemp()
        mock.patch.object(flow, 'RUNS_DIR', Path(self.tmp)).start()
        self.addCleanup(mock.patch.stopall)
        step = Path(self.tmp) / 'step.jsonl'
        step.write_text(''.join(json.dumps(r) + '\n' for r in [
            {'w': 'zzcat', 'rank': 1, 'reference': [{'pos': 'noun', 'glosses': ['a feline']}]}, {'w': 'zzxqv', 'rank': 2}, {'w': 'zzbad', 'rank': 3}]),
            encoding='utf-8')
        self.run_ = flow.Run.create('t', step, 'en', ['en', 'vi'], 'online')
        flow.stage_prepare(self.run_)
        self.ids = {w: i['id'] for w, i in self.run_.state['words'].items()}

    def _draft_results(self):
        return {self.ids['zzcat']: outcome(draft_json([SENSE])), self.ids['zzxqv']: outcome(draft_json([], ok=False)),
                self.ids['zzbad']: outcome('', stop='max_tokens')}

    def test_prepare_creates_processing_words(self):
        self.assertEqual(AIWord.objects.filter(value__in=self.ids, status='PROCESSING').count(), 3)

    def test_every_stage_reports_finished(self):
        """Giai đoạn không trả True thì advance coi là chưa xong và lặp mãi (lỗi đã gặp ở prepare)."""
        self.assertTrue(flow.stage_prepare(self.run_))

    def test_advance_runs_stages_in_order_and_refuses_a_second_runner(self):
        calls = []
        funcs = {name: (lambda run, n=name: calls.append(n) or True) for name in flow.STAGES}
        with mock.patch.dict(flow.STAGE_FUNCS, funcs):
            self.assertIsNone(flow.advance(self.run_, wait=False))
        self.assertEqual(calls, flow.STAGES)
        self.assertEqual(set(self.run_.state['stages'].values()), {'done'})
        (self.run_.dir / '.lock').write_text('x')
        with self.assertRaises(RuntimeError):
            flow.advance(self.run_, wait=False)

    def test_advance_stops_on_unfinished_batch_without_wait(self):
        funcs = {name: (lambda run: True) for name in flow.STAGES}
        funcs['draft'] = lambda run: False
        with mock.patch.dict(flow.STAGE_FUNCS, funcs):
            self.assertEqual(flow.advance(self.run_, wait=False), 'draft')
        self.assertEqual(self.run_.state['stages'].get('prepare'), 'done')
        self.assertNotIn('draft', self.run_.state['stages'])

    def test_draft_marks_rejected_and_failed_and_keeps_senses(self):
        with mock.patch.object(flow, '_batch_or_online', return_value=(self._draft_results(), True)) as run:
            self.assertTrue(flow.stage_draft(self.run_))
        words = self.run_.state['words']
        self.assertEqual([words[w]['status'] for w in ('zzcat', 'zzxqv', 'zzbad')], ['drafted', 'rejected', 'failed'])
        self.assertIn('max_tokens', words['zzbad']['reason'])
        self.assertEqual(self.run_.senses['zzcat'][0]['contents']['definition']['en']['value'], 'A small furry pet.')
        request = run.call_args.args[2][0]
        self.assertEqual(request['custom_id'], self.ids['zzcat'])
        self.assertIn('<reference>', request['params']['messages'][0]['content'])  # gợi ý đi vào prompt
        self.assertEqual(request['params']['thinking'], {'type': 'disabled'})

    def test_refine_applies_fixes_and_falls_back_to_draft(self):
        with mock.patch.object(flow, '_batch_or_online', return_value=(self._draft_results(), True)):
            flow.stage_draft(self.run_)
        sid = self.run_.senses['zzcat'][0]['id']
        fix = json.dumps({'senses': [{'id': sid, 'action': 'fix', 'definition': 'A small furry animal kept as a pet.', 'reason': 'clearer'}]})
        with mock.patch.object(flow, '_batch_or_online', return_value=({self.ids['zzcat']: outcome(fix)}, True)):
            flow.stage_refine(self.run_)
        sense = self.run_.senses['zzcat'][0]
        self.assertEqual(sense['contents']['definition']['en']['value'], 'A small furry animal kept as a pet.')
        self.assertEqual((sense['quality_status'], sense['ai_pass']), ('refined', 2))
        self.assertEqual(self.run_.state['words']['zzcat']['status'], 'refined')

        run2 = flow.Run.create('t2', Path(self.tmp) / 'step.jsonl', 'en', ['en', 'vi'], 'online')
        run2.senses['zzcat'] = json.loads(json.dumps(self.run_.senses['zzcat']))
        for w in run2.state['words']:
            run2.mark(w, 'drafted' if w == 'zzcat' else 'new')
            run2.state['words'][w]['id'] = self.ids[w]
        with mock.patch.object(flow, '_batch_or_online', return_value=({self.ids['zzcat']: outcome('not json')}, True)):
            flow.stage_refine(run2)
        self.assertEqual(run2.senses['zzcat'][0]['quality_status'], 'draft')  # refine hỏng: giữ draft
        self.assertIn('giữ nguyên draft', run2.state['words']['zzcat']['reason'])

    def test_save_writes_senses_and_closes_words(self):
        with mock.patch.object(flow, '_batch_or_online', return_value=(self._draft_results(), True)):
            flow.stage_draft(self.run_)
        self.run_.mark('zzcat', 'enriched')
        flow.stage_save(self.run_)
        self.assertEqual(AIWord.objects.get(value='zzcat').status, 'COMPLETED')
        self.assertEqual(AISense.objects.filter(word_value='zzcat').count(), 1)
        self.assertEqual(AIWord.objects.get(value='zzxqv').status, 'REJECTED')
        self.assertEqual(AIWord.objects.get(value='zzbad').status, 'FAILED')

    def test_save_again_overwrites_content_and_keeps_links(self):
        """Enrich lại một từ đã lưu: save lần hai trước đây IntegrityError (trùng id); nay ghi đè nội dung, giữ concept / ảnh / verify người."""
        from ai.models import AISenseMetadata, Concept
        with mock.patch.object(flow, '_batch_or_online', return_value=(self._draft_results(), True)):
            flow.stage_draft(self.run_)
        sid = self.run_.senses['zzcat'][0]['id']
        self.run_.senses['zzcat'][0]['metadata'] = {'id': str(uuid.uuid4()), 'tags': ['old']}
        self.run_.mark('zzcat', 'enriched')
        flow.stage_save(self.run_)
        concept = Concept.objects.create(label='cat', description_en='cat', embedding=b'x', member_count=1, status=Concept.ANCHORED)
        AISense.objects.filter(id=sid).update(concept=concept, verify=['vi'], ai_verify=['vi'], verify_state={'vi': {'state': 'ok'}}, views=7, image_preview='http://img')
        AISenseMetadata.objects.filter(senses__id=sid).update(image_link='http://m', verify=['*'])

        again = self.run_.senses['zzcat'][0]
        again['contents']['definition']['en']['value'] = 'New definition.'
        again['metadata']['tags'] = ['new']
        self.run_.mark('zzcat', 'enriched')
        flow.stage_save(self.run_)  # không lỗi

        sense = AISense.objects.get(id=sid)
        self.assertEqual(AISense.objects.filter(word_value='zzcat').count(), 1)
        self.assertEqual(sense.contents['definition']['en']['value'], 'New definition.')
        self.assertEqual((sense.concept_id, sense.verify, sense.views, sense.image_preview), (concept.id, ['vi'], 7, 'http://img'))
        self.assertEqual((sense.ai_verify, sense.verify_state), ([], {}))  # nội dung đổi: bước verify kiểm lại
        meta = AISenseMetadata.objects.get(senses__id=sid)
        self.assertEqual((meta.tags, meta.image_link, meta.verify), (['new'], 'http://m', ['*']))
        self.assertEqual(AIWord.objects.get(value='zzcat').status, 'COMPLETED')

    def test_missing_enrichment_reports_gaps(self):
        sense = {'pos': 'noun', 'contents': {'definition': {'en': {'value': 'x'}, 'vi': {'value': 'y'}}}, 'metadata': {'id': 'm'}}
        self.assertEqual(flow.missing_enrichment([sense], 'en', ['en', 'vi']), [])
        bare = {'pos': 'verb', 'contents': {'definition': {'en': {'value': 'x'}}}}
        self.assertEqual(flow.missing_enrichment([bare], 'en', ['en', 'vi']), ['verb: thiếu bản dịch vi', 'verb: thiếu metadata'])


class BatchResultTests(SimpleTestCase):
    def test_parse_json_rejects_truncated_or_invalid(self):
        self.assertEqual(llm.parse_json(outcome('{"a": 1}')), {'a': 1})
        self.assertIsNone(llm.parse_json(outcome('{"a": 1}', stop='max_tokens')))
        self.assertIsNone(llm.parse_json(outcome('nope')))
        self.assertIsNone(llm.parse_json(None))

    def test_batch_cost_is_half(self):
        self.assertAlmostEqual(llm.cost(1_000_000, 1_000_000, batch=True), llm.cost(1_000_000, 1_000_000) / 2)


class TranslatePromptWordTests(SimpleTestCase):
    def test_translate_prompt_names_the_word(self):
        """Trước đây chunk không có word_value nên prompt dịch ghi từ 'None'."""
        import asyncio
        from utils.ai import translate

        prompts = []

        class FakeModels:
            async def generate_content(self, model, contents, config):
                prompts.append(contents)
                raise RuntimeError('stop here')

        class FakeAio:
            models = FakeModels()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        client = mock.Mock(aio=FakeAio())
        sense = {'id': 'sid', 'word_value': 'whip', 'contents': {'definition': {'en': {'value': 'x'}}, 'examples': []}}
        chunk = {'s1': {'definition': 'x'}}
        with mock.patch.object(translate.genai, 'Client', return_value=client), mock.patch.object(translate, 'render_translate_schema', return_value={}):
            try:
                asyncio.run(translate.process_translation_chunk.__wrapped__(chunk, 'en', 'vi', ['vi'], {'s1': 'sid'}, {'sid': sense}, None))
            except Exception:
                pass
        self.assertTrue(prompts, 'không gọi Gemini')
        self.assertIn("word 'whip'", prompts[0])
        self.assertNotIn("'None'", prompts[0])


class SanitizeTests(SimpleTestCase):
    def test_removes_zero_width_and_foreign_script_translations(self):
        zw = chr(0x200b)
        sense = {'contents': {
            'definition': {'vi': {'value': f'ý kiến {zw}{zw}cụ thể '}},
            'usage': {'vi': {'value': f'dùng {zw}'}},
            'translations': {'ko': [{'value': '때문에'}, {'value': ' vì'}], 'ja': [{'value': 'that'}, {'value': 'その'}], 'zh': [{'value': 'i'}],
                             'vi': [{'value': 'rằng'}], 'es': [{'value': 'que'}]}}}
        fixed = flow.sanitize_senses([sense], 'en')
        c = sense['contents']
        self.assertEqual(c['definition']['vi']['value'], 'ý kiến cụ thể')
        self.assertEqual(c['usage']['vi']['value'], 'dùng')
        self.assertEqual(c['translations']['ko'], [{'value': '때문에'}])
        self.assertEqual(c['translations']['ja'], [{'value': 'その'}])
        self.assertEqual(c['translations']['zh'], [])
        self.assertEqual(c['translations']['vi'], [{'value': 'rằng'}])  # vi / es / fr dùng chữ Latin: giữ
        self.assertEqual(c['translations']['es'], [{'value': 'que'}])
        self.assertEqual(fixed['foreign_translation'], 3)
        self.assertGreaterEqual(fixed['zero_width'], 2)


class DedupeTests(SimpleTestCase):
    def test_dedupe_helper_keeps_order_drops_duplicates_and_caps(self):
        from utils.ai.lists import dedupe
        self.assertEqual(dedupe(['a', 'A ', 'b', '', 'b', 'c', 'd', 'e', 'f']), ['a', 'b', 'c', 'd', 'e'])
        self.assertEqual(dedupe([{'value': 'x'}, {'value': 'X'}, {'value': 'y'}]), [{'value': 'x'}, {'value': 'y'}])
        self.assertEqual(dedupe(None), None)

    def test_sanitize_dedupes_translations_and_metadata_lists(self):
        sense = {'contents': {'translations': {'vi': ['mèo', 'Mèo', 'con mèo'], 'zh': ['猫', '猫']}},
                 'metadata': {'tags': ['pet', 'pet'], 'image_keywords': ['cat'] * 3, 'advanced': {'synonyms': ['kitty', 'Kitty', 'puss'], 'idioms': []}}}
        fixed = flow.sanitize_senses([sense], 'en')
        self.assertEqual(sense['contents']['translations'], {'vi': ['mèo', 'con mèo'], 'zh': ['猫']})
        self.assertEqual(sense['metadata']['tags'], ['pet'])
        self.assertEqual(sense['metadata']['image_keywords'], ['cat'])
        self.assertEqual(sense['metadata']['advanced']['synonyms'], ['kitty', 'puss'])
        self.assertEqual(fixed['duplicate_items'], 1 + 1 + 1 + 2 + 1)
