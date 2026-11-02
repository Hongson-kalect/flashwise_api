"""Vận hành: backoff khi AI báo hết hạn mức, hàng đợi tin cậy của worker, lệnh seed_words."""
import json
import tempfile
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import mock

import redis.asyncio as aioredis
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from ai import tests as base
from ai.models import AIWord
from utils.ai import rate_limit
from utils.redis.word_init import WordCacheManager
from utils.utils.retry import retry_async


class ApiError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f'Error code: {status}')
        self.status_code = status
        self.response = SimpleNamespace(headers={'retry-after': str(retry_after)} if retry_after is not None else {})


class RateLimitTests(SimpleTestCase):
    def setUp(self):
        self.sleep = mock.patch('utils.ai.rate_limit.asyncio.sleep', mock.AsyncMock()).start()
        self.cooldown = mock.patch('utils.ai.rate_limit.set_cooldown').start()
        self.addCleanup(mock.patch.stopall)

    def test_transient_detection(self):
        self.assertTrue(rate_limit.is_transient(ApiError(429)))
        self.assertTrue(rate_limit.is_transient(ApiError(529)))
        self.assertTrue(rate_limit.is_transient(Exception('429 RESOURCE_EXHAUSTED quota')))  # lỗi Gemini dạng chuỗi
        self.assertTrue(rate_limit.is_transient(TimeoutError()))
        self.assertFalse(rate_limit.is_transient(ApiError(400)))
        self.assertFalse(rate_limit.is_transient(ValueError('bad json')))

    @override_settings(AI_BACKOFF_BASE_SECONDS=2, AI_BACKOFF_MAX_SECONDS=60)
    def test_backoff_uses_retry_after_and_cap(self):
        self.assertAlmostEqual(rate_limit.backoff_seconds(ApiError(429, retry_after=7), 1), 7.5, delta=0.5)
        self.assertAlmostEqual(rate_limit.backoff_seconds(ApiError(503), 3), 8.5, delta=0.5)
        self.assertLessEqual(rate_limit.backoff_seconds(ApiError(503), 20), 61)

    @override_settings(AI_RATE_LIMIT_RETRIES=3)
    def test_call_with_backoff(self):
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ApiError(429)
            return 'ok'

        self.assertEqual(async_to_sync(rate_limit.call_with_backoff)(flaky, 'anthropic'), 'ok')
        self.assertEqual((len(calls), self.sleep.await_count, self.cooldown.call_count), (3, 2, 2))

        async def broken():
            raise ApiError(400)

        with self.assertRaises(ApiError):
            async_to_sync(rate_limit.call_with_backoff)(broken, 'anthropic')
        self.assertEqual(self.sleep.await_count, 2)  # lỗi logic không chờ

    @override_settings(AI_RATE_LIMIT_RETRIES=2)
    def test_retry_async_gives_up(self):
        calls = []

        @retry_async()
        async def always_busy():
            calls.append(1)
            raise ApiError(503)

        with self.assertRaises(ApiError):
            async_to_sync(always_busy)()
        self.assertEqual(len(calls), 3)  # 1 lần đầu + 2 lần thử lại

        @retry_async()
        async def hangs():
            calls.append(1)
            raise TimeoutError()

        with self.assertRaises(TimeoutError):
            async_to_sync(hangs)()
        self.assertEqual(len(calls), 4)  # quá hạn thời gian: không thử lại


class CooldownRedisTests(SimpleTestCase):
    def test_cooldown_only_extends(self):
        key = rate_limit.COOLDOWN_KEY.format('test-provider')
        self.addCleanup(rate_limit._redis().delete, key)
        rate_limit.set_cooldown('test-provider', 5)
        rate_limit.set_cooldown('test-provider', 1)  # không rút ngắn
        self.assertGreater(rate_limit.cooldown_remaining('test-provider'), 3)
        self.assertEqual(rate_limit.cooldown_remaining('nobody'), 0)


class DraftBackoffTests(base.PipelineBase):
    run_pipeline = base.PipelineTests.run_pipeline

    def test_draft_waits_on_429_then_succeeds_with_base_languages(self):
        attempts = []

        def script(model):
            attempts.append(model)
            if len(attempts) == 1:
                raise ApiError(429, retry_after=1)
            return base.stream_pieces(base.DRAFT)

        seen = []
        translate = self.fake_translate

        async def record(props, translate_base_language=True):
            seen.append(props.get('base_languages'))
            return await translate(props, translate_base_language)

        self.fake_translate = record
        self.job.update(base_languages=['en', 'ja'], no_image=True)
        with mock.patch('utils.ai.rate_limit.asyncio.sleep', mock.AsyncMock()) as sleep, \
                mock.patch('utils.ai.rate_limit.set_cooldown') as cooldown, \
                mock.patch('utils.ai.word_render.fetch_sense_image') as image:
            result = self.run_pipeline(script, mock.AsyncMock(return_value=None))
        self.assertTrue(result['is_valid'])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])  # thử lại cùng model, không bỏ sang model khác
        sleep.assert_awaited_once()
        cooldown.assert_called_once()
        self.assertEqual(seen, [['en', 'ja'], ['en', 'ja']])  # lô seed dịch sẵn các ngôn ngữ chính
        image.assert_not_called()
        self.assertEqual(AIWord.objects.get(pk=self.word.pk).status, 'COMPLETED')

    @override_settings(AI_RATE_LIMIT_RETRIES=1)
    def test_draft_gives_up_after_retries(self):
        def script(model):
            raise ApiError(429)

        with mock.patch('utils.ai.rate_limit.asyncio.sleep', mock.AsyncMock()), mock.patch('utils.ai.rate_limit.set_cooldown'):
            result = self.run_pipeline(script, mock.AsyncMock(return_value=None))
        self.assertFalse(result['is_valid'])
        self.assertEqual(AIWord.objects.get(pk=self.word.pk).status, 'FAILED')


class RefineBackoffTests(SimpleTestCase):
    def test_refine_retries_same_model_on_429(self):
        from utils.ai import refine
        responses = [ApiError(429), SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(type='text', text='{"senses": []}')])]

        async def create(**kwargs):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        class Client:
            messages = SimpleNamespace(create=create)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        with mock.patch('utils.ai.claude.client', lambda: Client()), \
                mock.patch('utils.ai.rate_limit.asyncio.sleep', mock.AsyncMock()), mock.patch('utils.ai.rate_limit.set_cooldown'), \
                override_settings(AI_REFINE_MODELS=['only-model']):
            result = async_to_sync(refine.request_refinement)('prompt')
        self.assertEqual(result, {'data': {'senses': []}, 'model': 'only-model'})


class WorkerQueueTests(TestCase):
    def setUp(self):
        from utils.celery import worker
        self.worker = worker

    def test_requeue_orphans_of_dead_worker(self):
        worker = self.worker

        async def scenario():
            client = aioredis.from_url(f'redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/0', decode_responses=True)
            dead, alive = worker.processing_key('test_q', 'dead:1'), worker.processing_key('test_q', 'alive:2')
            try:
                await client.delete('test_q', dead, alive, worker.HEARTBEAT_KEY.format('alive:2'))
                await client.rpush(dead, json.dumps({'value': 'cat'}), json.dumps({'value': 'boom', '_requeued': settings.JOB_MAX_REQUEUES}))
                await client.rpush(alive, json.dumps({'value': 'busy'}))
                await client.set(worker.HEARTBEAT_KEY.format('alive:2'), 1, ex=30)
                moved = await worker.requeue_orphans(client)
                queue = [json.loads(raw) for raw in await client.lrange('test_q', 0, -1)]
                return moved, queue, await client.llen(dead), await client.llen(alive)
            finally:
                await client.delete('test_q', dead, alive, worker.HEARTBEAT_KEY.format('alive:2'))
                await client.close()

        moved, queue, dead_left, alive_left = async_to_sync(scenario)()
        self.assertEqual(moved, 1)
        self.assertEqual(queue, [{'value': 'cat', '_requeued': 1}])  # job làm sập worker quá nhiều lần thì bỏ
        self.assertEqual((dead_left, alive_left), (0, 1))  # worker còn sống không bị lấy job

    def test_word_already_done(self):
        word = AIWord.objects.create(value='cat', language_code='en', status='COMPLETED')
        self.assertTrue(self.worker.word_already_done({'word_id': str(word.id)}))
        self.assertFalse(self.worker.word_already_done({'word_id': str(word.id), 'replace_sense_ids': ['x']}))
        AIWord.objects.filter(pk=word.pk).update(status='PROCESSING')
        self.assertFalse(self.worker.word_already_done({'word_id': str(word.id)}))


class SeedWordsTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('root', password='x')
        self.push = mock.patch('ai.generation.push_job').start()
        mock.patch('ai.management.commands.seed_words.queue_length', return_value=0).start()
        mock.patch('ai.management.commands.seed_words.cooldown_remaining', return_value=0).start()
        self.sleep = mock.patch('ai.management.commands.seed_words.time.sleep').start()
        self.addCleanup(mock.patch.stopall)
        self.cache = WordCacheManager()
        for word in ('zzcat', 'zzdog', 'zzfail', 'zzbusy'):
            self.addCleanup(self.cache.cache_word_clear_specific, 'en', word)
            self.cache.cache_word_clear_specific('en', word)
        AIWord.objects.create(value='zzcat', language_code='en', status='COMPLETED')
        self.failed = AIWord.objects.create(value='zzfail', language_code='en', status='FAILED')
        AIWord.objects.create(value='zzbusy', language_code='en', status='PROCESSING')
        self.file = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8')
        self.file.write('# danh sách thử\nzzcat\n  ZZDog\tnoun\nzzdog\n!!!\n\nzzfail\nzzbusy\n')
        self.file.close()

    def run_seed(self, *args):
        out = StringIO()
        call_command('seed_words', self.file.name, '--lang', 'en', '--base-languages', 'en,vi,ja', *args, stdout=out)
        return out.getvalue()

    def test_dry_run(self):
        out = self.run_seed('--dry-run')
        self.assertIn("'queued': 1, 'retried': 1, 'skipped': 2", out)
        self.push.assert_not_called()

    def test_seed_queues_new_and_failed_words(self):
        out = self.run_seed('--rate', '120')
        self.assertIn("'invalid': 1", out)
        jobs = [c.args[1] for c in self.push.call_args_list]
        self.assertEqual([j['value'] for j in jobs], ['zzdog', 'zzfail'])
        self.assertEqual(jobs[0]['base_languages'], ['en', 'vi', 'ja'])
        self.assertTrue(jobs[0]['no_image'])
        self.assertEqual(jobs[1]['word_id'], str(self.failed.id))  # tạo lại trên cùng bản ghi
        self.assertEqual(AIWord.objects.get(pk=self.failed.pk).status, 'PROCESSING')
        self.sleep.assert_called_with(0.5)  # 120 từ / phút

        # Chạy lại: các từ đang tạo không bị đẩy lần 2
        self.push.reset_mock()
        self.run_seed()
        self.push.assert_not_called()

    def test_stale_processing_is_retried(self):
        AIWord.objects.filter(value='zzbusy').update(updated_at=timezone.now() - timedelta(hours=1))
        self.run_seed()
        self.assertIn('zzbusy', [c.args[1]['value'] for c in self.push.call_args_list])
