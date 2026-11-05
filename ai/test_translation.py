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
