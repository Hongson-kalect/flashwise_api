import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache as django_cache
from django.test import override_settings
from rest_framework.test import APITestCase
from uuid6 import uuid7

from ai.models import AISense, AIWord
from ai.tests import FakeQueue, make_sense
from ai.translation import collect_gaps, missing_sense_ids, queue_gaps
from utils.celery.worker import save_translated_contents

User = get_user_model()
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class JsonQueue(FakeQueue):
    def push_job(self, queue, payload):
        json.dumps(payload)
        super().push_job(queue, payload)


@override_settings(CACHES=LOCMEM_CACHE)
class DeltaTranslationTests(APITestCase):
    """Nghĩa user sửa (delta) cũng được dịch: phần sửa ghi vào delta của chính nghĩa đó, không đụng nghĩa gốc."""

    def setUp(self):
        django_cache.clear()
        self.queue = JsonQueue()
        for target in ('ai.jobs.push_job', 'ai.jobs.acquire_lock'):
            patcher = mock.patch(target, getattr(self.queue, target.rsplit('.', 1)[1]))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alice = User.objects.create_user('alice')
        self.word = AIWord.objects.create(value='bank', language_code='en', status='COMPLETED')
        data = make_sense(str(uuid7()))
        data.update(word_id=self.word.id, word_value='bank')
        self.official = AISense.objects.create(**data)  # có en + vi đầy đủ
        self.custom = AISense.objects.create(
            word=self.word, word_value='bank', language_code='en', is_official=False, created_by=self.alice,
            original=self.official, contents={}, delta={'definition': {'vi': {'value': 'Chỗ giữ tiền của tôi'}}},
        )

    def test_delta_text_missing_for_other_language_is_queued_against_the_custom_sense(self):
        gaps = collect_gaps([self.custom], 'ja')
        delta_gap = next(g for g in gaps if g.delta)
        self.assertEqual((delta_gap.sense_id, delta_gap.source), (str(self.custom.id), 'vi'))
        queue_gaps(gaps, 'ja')
        jobs = [job for _, job in self.queue.jobs]
        delta_job = next(job for job in jobs if job['language_code'] == 'vi')
        self.assertEqual(delta_job['current_senses'][0]['target'], 'delta')
        self.assertEqual(delta_job['current_senses'][0]['contents']['definition']['vi']['value'], 'Chỗ giữ tiền của tôi')
        self.assertEqual(delta_job['user_language_code'], 'ja')

    def test_viewer_in_editor_language_has_no_delta_gap(self):
        self.assertFalse(any(g.delta for g in collect_gaps([self.custom], 'vi')))

    def test_save_translation_goes_into_delta_not_original(self):
        before = AISense.objects.get(pk=self.official.pk).contents
        save_translated_contents(
            [{'id': str(self.custom.id), 'target': 'delta', 'contents': {'definition': {'vi': {'value': 'x'}, 'ja': {'value': '私の銀行'}}}}],
            'vi',
        )
        self.custom.refresh_from_db()
        self.assertEqual(self.custom.delta['definition']['ja']['value'], '私の銀行')
        self.assertEqual(self.custom.delta['definition']['vi']['value'], 'Chỗ giữ tiền của tôi')  # nội dung gốc giữ nguyên
        self.assertEqual(AISense.objects.get(pk=self.official.pk).contents, before)
        self.assertFalse(any(g.delta for g in collect_gaps([self.custom], 'ja')))

    def test_translate_endpoint_queues_and_reports_pending_and_ready(self):
        self.client.force_authenticate(self.alice)
        res = self.client.post('/api/ai-senses/translate/', {'sense_ids': [str(self.official.id), str(self.custom.id)], 'user_lang': 'ja'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.data['pending']), {str(self.official.id), str(self.custom.id)})
        self.assertTrue(self.queue.jobs)
        res = self.client.post('/api/ai-senses/translate/', {'sense_ids': [str(self.official.id)], 'user_lang': 'vi'}, format='json')
        self.assertEqual((res.data['ready'], res.data['pending']), ([str(self.official.id)], []))
        self.assertEqual(self.client.post('/api/ai-senses/translate/', {'sense_ids': [], 'user_lang': 'ja'}, format='json').status_code, 400)

    def test_missing_ids_are_unique(self):
        gaps = collect_gaps([self.custom], 'ja')
        self.assertEqual(missing_sense_ids(gaps), [str(self.custom.id)])

    def test_public_collection_detail_reports_translation_coverage(self):
        from core.models import Collection, CollectionItem

        collection = Collection.objects.create(name='c', language_code='en', released=True, version=1, created_by=self.alice)
        for sense in (self.official, self.custom):
            CollectionItem.objects.create(collection=collection, sense=sense, original=sense, released=True, version=1, value='bank')
        self.client.force_authenticate(self.alice)
        res = self.client.get(f'/api/collections/{collection.id}/', {'user_lang': 'ja'})
        self.assertEqual(res.data['translation'], {'language': 'ja', 'total': 2, 'missing': 2})
        res = self.client.get(f'/api/collections/{collection.id}/', {'user_lang': 'vi'})
        self.assertEqual(res.data['translation'], {'language': 'vi', 'total': 2, 'missing': 0})
        self.assertIsNone(self.client.get(f'/api/collections/{collection.id}/').data['translation'])


@override_settings(CACHES=LOCMEM_CACHE, POPULAR_LANGUAGES=['en', 'vi', 'ja'], LAZY_TRANS_MAX_QUEUE=100, LAZY_TRANS_ENABLED=True)
class LazyTranslationTests(APITestCase):
    """Dịch nền: hàng đợi riêng, chỉ cặp ngôn ngữ phổ biến, không xếp trùng."""

    def setUp(self):
        django_cache.clear()
        self.queue = JsonQueue()
        for target in ('ai.jobs.push_job', 'ai.jobs.acquire_lock'):
            patcher = mock.patch(target, getattr(self.queue, target.rsplit('.', 1)[1]))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch('ai.jobs.lazy_queue_length', lambda: 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.alice = User.objects.create_user('alice')
        word = AIWord.objects.create(value='bank', language_code='en', status='COMPLETED')
        data = make_sense(str(uuid7()))
        data.update(word_id=word.id, word_value='bank')
        self.sense = AISense.objects.create(**data)

    def test_popular_pair_goes_to_lazy_queue_only(self):
        gaps = collect_gaps([self.sense], 'ja')
        queue_gaps(gaps, 'ja', lazy=True)
        self.assertEqual(self.queue.names(), ['redis_trans_lazy'])
        job = self.queue.jobs[0][1]
        self.assertTrue(job['lazy'])
        self.assertEqual((job['language_code'], job['user_language_code']), ('en', 'ja'))
        queue_gaps(gaps, 'ja', lazy=True)
        self.assertEqual(self.queue.names(), ['redis_trans_lazy'])  # đã xếp rồi: không xếp trùng

    def test_disabled_by_default_flag_queues_nothing(self):
        with override_settings(LAZY_TRANS_ENABLED=False):
            queue_gaps(collect_gaps([self.sense], 'ja'), 'ja', lazy=True)
        self.assertEqual(self.queue.jobs, [])

    def test_unpopular_language_is_not_queued(self):
        queue_gaps(collect_gaps([self.sense], 'sw'), 'sw', lazy=True)
        self.assertEqual(self.queue.jobs, [])

    def test_full_lazy_queue_refuses_new_jobs(self):
        with mock.patch('ai.jobs.lazy_queue_length', lambda: 100):
            queue_gaps(collect_gaps([self.sense], 'ja'), 'ja', lazy=True)
        self.assertEqual(self.queue.jobs, [])

    def test_bundle_download_queues_lazy_not_urgent(self):
        from core.models import Collection, CollectionItem, UserCollection

        collection = Collection.objects.create(name='c', language_code='en', released=True, version=1, created_by=self.alice)
        CollectionItem.objects.create(collection=collection, sense=self.sense, original=self.sense, released=True, version=1, value='bank')
        uc = UserCollection.objects.create(user=self.alice, collection=collection, version=1)
        self.client.force_authenticate(self.alice)
        res = self.client.get(f'/api/user-collections/{uc.id}/bundle/', {'user_lang': 'ja'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.queue.names(), ['redis_trans_lazy'])  # tải bộ không đẩy việc gấp
        self.assertEqual(res.data['user_collection']['translation_missing'], 1)

    def test_worker_waits_while_urgent_work_exists(self):
        import asyncio

        from utils.celery.worker import lazy_should_wait

        idle = asyncio.Semaphore(3)
        self.assertFalse(lazy_should_wait([0, 0, 0, 0], [(idle, 3)]))
        self.assertTrue(lazy_should_wait([0, 2, 0, 0], [(idle, 3)]))  # còn việc gấp trong hàng đợi

        async def busy():
            running = asyncio.Semaphore(3)
            await running.acquire()
            return lazy_should_wait([0, 0, 0, 0], [(running, 3)])

        self.assertTrue(asyncio.run(busy()))  # có việc gấp đang chạy
