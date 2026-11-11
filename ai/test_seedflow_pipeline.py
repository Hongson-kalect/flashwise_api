"""Hàng đợi concept Redis (utils/concept/queue.py), stage concept xếp hàng và bộ điều phối pipeline lệch pha."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from ai.models import AISense, AIWord
from ai.seedflow import flow
from utils.concept import queue
from utils.redis.client import get_queue_client

TEST_PREFIX = 'concept_arr_test'


class RedisCase:
    def setUpRedis(self):
        mock.patch.object(queue, 'PREFIX', TEST_PREFIX).start()
        self.addCleanup(mock.patch.stopall)
        self.redis = get_queue_client()
        self.clean()
        self.addCleanup(self.clean)

    def clean(self):
        for key in self.redis.scan_iter(f'{TEST_PREFIX}*'):
            self.redis.delete(key)


class QueueTests(RedisCase, SimpleTestCase):
    def setUp(self):
        self.setUpRedis()

    def test_fifo_and_ack(self):
        queue.push('en', ['a', 'b', 'c'])
        seen = []
        result = queue.consume('en', lambda sid: seen.append(sid) or {'outcome': 'created'})
        self.assertEqual(seen, ['a', 'b', 'c'])
        self.assertEqual(result['processed'], 3)
        self.assertEqual(result['outcomes'], {'created': 3})
        self.assertEqual(queue.depth('en'), {'waiting': 0, 'busy': 0, 'failed': 0, 'done': {'created': 3}})

    def test_languages_are_separate_queues(self):
        queue.push('en', ['a'])
        queue.push('vi', ['b'])
        self.assertEqual(queue.languages(), ['en', 'vi'])
        seen = []
        queue.consume('vi', lambda sid: seen.append(sid) or {'outcome': 'x'})
        self.assertEqual(seen, ['b'])
        self.assertEqual(queue.depth('en')['waiting'], 1)
        self.assertEqual(queue.languages(), ['en'])

    def test_error_goes_to_failed_and_does_not_block(self):
        queue.push('en', ['a', 'bad', 'c'])

        def link(sid):
            if sid == 'bad':
                raise RuntimeError('judge down')
            return {'outcome': 'created'}
        result = queue.consume('en', link)
        self.assertEqual(result['outcomes'], {'created': 2, 'failed': 1})
        info = queue.depth('en')
        self.assertEqual((info['waiting'], info['busy'], info['failed']), (0, 0, 1))
        self.assertEqual(queue.requeue_failed('en'), 1)
        self.assertEqual(queue.depth('en')['waiting'], 1)

    def test_dead_consumer_work_is_requeued_in_order(self):
        queue.push('en', ['a', 'b', 'c'])
        queue.claim('en')
        queue.claim('en')  # consumer chết khi đang giữ a, b
        self.assertEqual(queue.depth('en')['busy'], 2)
        seen = []
        queue.consume('en', lambda sid: seen.append(sid) or {'outcome': 'x'})
        self.assertEqual(seen, ['a', 'b', 'c'])

    def test_limit_and_idle_wait(self):
        queue.push('en', ['a', 'b', 'c'])
        self.assertEqual(queue.consume('en', lambda sid: {'outcome': 'x'}, limit=2)['processed'], 2)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            queue.consume('en', lambda sid: {'outcome': 'x'}, stop_when_empty=False, idle_seconds=7, sleep=sleep)
        self.assertEqual(sleeps, [7, 7])


@override_settings(SEED_CONCEPT_QUEUE=True)
class ConceptStageQueueTests(RedisCase, TestCase):
    def setUp(self):
        self.setUpRedis()
        user = get_user_model().objects.create_superuser('root', password='x')
        self.tmp = tempfile.mkdtemp()
        mock.patch.object(flow, 'RUNS_DIR', Path(self.tmp)).start()
        step = Path(self.tmp) / 'step.jsonl'
        step.write_text(json.dumps({'w': 'zzqueue', 'rank': 1}) + '\n', encoding='utf-8')
        self.run_ = flow.Run.create('q', step, 'en', ['en', 'vi'], 'batch')
        word = AIWord.objects.create(value='zzqueue', language_code='en', created_by=user, status='COMPLETED')
        self.run_.state['words']['zzqueue'].update(id=str(word.id), status='saved')
        self.senses = [AISense.objects.create(word=word, word_value='zzqueue', language_code='en', created_by=user, is_official=True, pos='noun') for _ in range(2)]
        AISense.objects.create(word=word, word_value='zzqueue', language_code='en', created_by=user, is_official=False)

    def test_stage_pushes_official_unlinked_senses_and_finishes(self):
        self.assertTrue(flow.stage_concept(self.run_))
        self.assertEqual(self.run_.state['concept_queued'], {'en': 2})
        pushed = {self.redis.lpop(queue.queue_key('en')).decode() for _ in range(2)}
        self.assertEqual(pushed, {str(s.id) for s in self.senses})
        self.assertIsNone(self.redis.lpop(queue.queue_key('en')))

    def test_flag_off_keeps_inline_linking(self):
        with override_settings(SEED_CONCEPT_QUEUE=False), mock.patch('utils.concept.linker.link_word', return_value=[{'outcome': 'created'}]) as link:
            self.assertTrue(flow.stage_concept(self.run_))
        link.assert_called_once()
        self.assertEqual(queue.depth('en')['waiting'], 0)


class FakeRun:
    """Run giả: mỗi lần advance đi 1 stage; waiting_for = số lần advance phải trả 'đang chờ' trước khi xong stage."""

    def __init__(self, name, stages=('draft', 'refine', 'save')):
        self.name, self.plan = name, list(stages)
        self.state = {'words': {'w': {}}, 'stages': {}}


class PipelineTests(SimpleTestCase):
    def setUp(self):
        self.log = []
        self.say = self.log.append

    def fake_advance(self, order):
        def advance(run, wait=False, **kw):
            order.append(run.name)
            if run.plan:
                run.state['stages'][run.plan.pop(0)] = 'done'
            return run.plan[0] if run.plan else None
        return advance

    def test_runs_overlap_one_stage_apart(self):
        runs = [FakeRun('a'), FakeRun('b'), FakeRun('c')]
        feed = iter(runs)
        order = []
        with mock.patch.object(flow, 'advance', self.fake_advance(order)):
            result = flow.pipeline(lambda: next(feed, None), max_active=2, poll_seconds=0, sleep=lambda s: None, say=self.say)
        self.assertEqual(result, {'done': ['a', 'b', 'c'], 'dropped': []})
        # b chỉ được nhận sau khi a qua draft; trước khi a xong, b đã chạy xen kẽ
        self.assertLess(order.index('b'), len(order) - order[::-1].index('a') - 1)
        self.assertEqual(order[0], 'a')

    def test_max_active_one_is_sequential(self):
        runs = [FakeRun('a'), FakeRun('b')]
        feed = iter(runs)
        order = []
        with mock.patch.object(flow, 'advance', self.fake_advance(order)):
            flow.pipeline(lambda: next(feed, None), max_active=1, poll_seconds=0, sleep=lambda s: None, say=self.say)
        self.assertEqual(order, ['a', 'a', 'a', 'b', 'b', 'b'])

    def test_failing_run_is_dropped_after_max_errors_others_continue(self):
        runs = [FakeRun('bad'), FakeRun('ok')]
        feed = iter(runs)
        good = self.fake_advance([])

        def advance(run, **kw):
            if run.name == 'bad':
                raise RuntimeError('hỏng')
            return good(run, **kw)
        sleeps = []
        with mock.patch.object(flow, 'advance', advance):
            result = flow.pipeline(lambda: next(feed, None), max_active=2, poll_seconds=0, sleep=sleeps.append, say=self.say, max_errors=2)
        self.assertEqual(result, {'done': ['ok'], 'dropped': ['bad']})

    def test_sleeps_between_sweeps_and_stops_when_empty(self):
        feed = iter([FakeRun('a', ('draft', 'refine'))])
        sleeps = []
        with mock.patch.object(flow, 'advance', self.fake_advance([])):
            flow.pipeline(lambda: next(feed, None), poll_seconds=45, sleep=sleeps.append, say=self.say)
        self.assertEqual(sleeps, [45])
