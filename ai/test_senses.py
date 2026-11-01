import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase
from rest_framework.throttling import AnonRateThrottle
from uuid6 import uuid7

from ai.models import AISense, AISenseMetadata, AIWord
from ai.tests import FakeQueue, make_sense
from ai.validators import clean_contents, clean_metadata, is_lang, is_uuid

User = get_user_model()

LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class JsonQueue(FakeQueue):
    """Như hàng đợi thật: job phải serialize được thành JSON (bản cũ đẩy cả model instance nên không bao giờ đẩy được)."""

    def push_job(self, queue, payload):
        json.dumps(payload)
        super().push_job(queue, payload)


class ValidatorTests(TestCase):
    def test_is_uuid_and_lang(self):
        self.assertTrue(is_uuid(str(uuid7())))
        for bad in (None, '', 'nope', 5, '123'):
            self.assertFalse(is_uuid(bad))
        for good in ('en', 'vi', 'zh-CN', 'pt_BR'):
            self.assertTrue(is_lang(good))
        for bad in (None, '', 'e', 'english', 'en;drop', 5):
            self.assertFalse(is_lang(bad))

    def test_clean_contents_keeps_only_known_shape(self):
        cleaned = clean_contents({
            'definition': {'en': {'value': '  A bank. ', 'evil': 1, 'roman': 'x'}, 'toolongcode': {'value': 'x'}, 'vi': {'value': ''}},
            'usage': 'not a dict',
            'examples': [{'en': {'value': 'ex'}}, 'bad', {'en': {'value': ''}}] + [{'en': {'value': 'e'}}] * 10,
            'translations': {'vi': ['ngân hàng', 5, ' '], 'xx': 'no'},
            '__proto__': {'polluted': True},
            'image': 'http://x',
        })
        self.assertEqual(cleaned['definition'], {'en': {'value': 'A bank.', 'roman': 'x'}})
        self.assertNotIn('usage', cleaned)
        self.assertEqual(len(cleaned['examples']), 3)  # chỉ 5 phần tử đầu được xét, trong đó 'bad' và ví dụ rỗng bị bỏ
        self.assertEqual(cleaned['translations'], {'vi': ['ngân hàng']})
        self.assertEqual(set(cleaned), {'definition', 'examples', 'translations'})

    def test_clean_contents_garbage(self):
        for garbage in (None, 5, 'x', [], {'definition': []}):
            self.assertEqual(clean_contents(garbage), {})

    def test_clean_metadata_drops_forged_fields(self):
        cleaned = clean_metadata({'id': 'forged', 'created_by': 1, 'tags': ['a', 5, ' b '], 'image_link': 'javascript:alert(1)', 'advanced': {'k': 1}})
        self.assertEqual(cleaned, {'tags': ['a', 'b'], 'advanced': {'k': 1}})


@override_settings(CACHES=LOCMEM_CACHE)
class SenseApiTests(APITestCase):
    def setUp(self):
        django_cache.clear()
        self.queue = JsonQueue()
        for target in ('ai.jobs.push_job', 'ai.jobs.acquire_lock'):
            patcher = mock.patch(target, getattr(self.queue, target.rsplit('.', 1)[1]))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.admin = User.objects.create_user('admin-user', is_staff=True)
        self.word = AIWord.objects.create(value='bank', language_code='en', status='COMPLETED')

    def sense(self, **overrides):
        data = make_sense(str(uuid7()))
        data.update(word_id=self.word.id, word_value='bank')
        data['contents']['definition'].pop('vi')  # chỉ có ngôn ngữ gốc, thiếu bản dịch
        data['contents'].pop('translations')
        data['contents']['examples'][0].pop('vi')
        data.update(overrides)
        return AISense.objects.create(**data)

    def login(self, user):
        self.client.force_authenticate(user)

    # ---------- xem ----------

    def test_retrieve_returns_user_language_content_and_queues_translation(self):
        official = self.sense()
        res = self.client.get(f'/api/ai-senses/{official.id}/', {'user_lang': 'vi'})
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.data['contents'], dict)
        self.assertEqual(res.data['contents']['definition']['en']['value'], 'A financial institution.')
        self.assertEqual(self.queue.names(), ['redis_trans'])  # trước đây json.dumps(model instance) lỗi và bị nuốt nên không bao giờ đẩy được
        job = self.queue.jobs[0][1]
        self.assertEqual(job['current_senses'][0]['id'], str(official.id))
        self.assertEqual(job['user_language_code'], 'vi')

        self.client.get(f'/api/ai-senses/{official.id}/', {'user_lang': 'vi'})
        self.assertEqual(self.queue.names(), ['redis_trans'])  # lần 2 không đẩy trùng

    def test_delta_sense_translation_goes_to_original(self):
        official = self.sense()
        custom = AISense.objects.create(word=self.word, word_value='bank', language_code='en', is_official=False,
                                        created_by=self.alice, original=official, delta={'x': 1}, contents={})
        self.login(self.alice)
        res = self.client.get(f'/api/ai-senses/{custom.id}/', {'user_lang': 'vi'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['contents']['definition']['en']['value'], 'A financial institution.')
        self.assertEqual(self.queue.jobs[0][1]['missing_translate'][0]['id'], str(official.id))  # bản dịch ghi vào nghĩa gốc

    def test_sense_without_contents_or_original_does_not_crash(self):
        bare = AISense.objects.create(word=self.word, word_value='bank', language_code='en', is_official=False, created_by=self.alice, contents={})
        self.login(self.alice)
        res = self.client.get(f'/api/ai-senses/{bare.id}/')
        self.assertEqual((res.status_code, res.data['contents']), (200, {}))

    def test_invalid_user_lang(self):
        self.assertEqual(self.client.get(f'/api/ai-senses/{self.sense().id}/', {'user_lang': 'x;y'}).status_code, 400)

    def test_custom_senses_are_private(self):
        mine = self.sense(is_official=False, created_by=self.alice)
        official = self.sense()

        self.assertEqual(self.client.get(f'/api/ai-senses/{mine.id}/').status_code, 404)  # chưa đăng nhập
        self.login(self.bob)
        self.assertEqual(self.client.get(f'/api/ai-senses/{mine.id}/').status_code, 404)
        self.login(self.alice)
        self.assertEqual(self.client.get(f'/api/ai-senses/{mine.id}/').status_code, 200)
        self.login(self.admin)
        self.assertEqual(self.client.get(f'/api/ai-senses/{mine.id}/').status_code, 200)

        self.login(self.bob)
        ids = {row['id'] for row in self.client.get('/api/ai-senses/').data['results']}
        self.assertEqual(ids, {str(official.id)})

    # ---------- tạo ----------

    def create(self, **body):
        payload = {'word_id': str(self.word.id), 'user_lang': 'vi',
                   'contents': {'definition': {'en': {'value': 'A place for money.'}}},
                   'metadata': {'tags': ['finance']}}
        payload.update(body)
        return self.client.post('/api/ai-senses/create-sense/', payload, format='json')

    def test_create_sense_validates_input(self):
        self.login(self.alice)
        self.assertEqual(self.create(word_id='nope').status_code, 400)
        self.assertEqual(self.create(user_lang='x;y').status_code, 400)
        self.assertEqual(self.create(contents={'definition': {'vi': {'value': 'chỉ có tiếng Việt'}}}).status_code, 400)
        self.assertEqual(self.create(metadata='x').status_code, 400)
        self.assertEqual(self.create(word_id=str(uuid7())).status_code, 404)
        self.assertEqual(AISense.objects.count(), 0)

    def test_create_sense_cannot_forge_ids_or_owner(self):
        victim = AISenseMetadata.objects.create(tags=['original'])
        self.login(self.alice)
        res = self.create(metadata={'id': str(victim.id), 'created_by': self.bob.id, 'tags': ['mine']})
        self.assertEqual(res.status_code, 206)

        victim.refresh_from_db()
        self.assertEqual(victim.tags, ['original'])  # trước đây AISenseMetadata(**metadata).save() ghi đè metadata của nghĩa khác
        sense = AISense.objects.get(id=res.data['data']['id'])
        self.assertNotEqual(sense.metadata_id, victim.id)
        self.assertEqual((sense.created_by_id, sense.metadata.created_by_id), (self.alice.id, self.alice.id))
        self.assertEqual(sense.metadata.tags, ['mine'])
        self.assertFalse(sense.is_official)

    def test_create_sense_stores_only_clean_contents_and_queues_missing(self):
        self.login(self.alice)
        res = self.create(contents={'definition': {'en': {'value': 'A place for money.', 'evil': 1}}, 'junk': {'a': 1}})
        self.assertEqual(res.status_code, 206)
        sense = AISense.objects.get(id=res.data['data']['id'])
        self.assertEqual(sense.contents, {'definition': {'en': {'value': 'A place for money.'}}})
        self.assertEqual(self.queue.names(), ['redis_trans'])
        self.assertEqual(self.queue.jobs[0][1]['missing_translate'][0]['id'], str(sense.id))

    # ---------- sửa ----------

    def update(self, sense, **body):
        payload = {'sense_id': str(sense.id), 'user_lang': 'vi', 'delta': {'definition': 'mine'}}
        payload.update(body)
        return self.client.put('/api/ai-senses/update-sense/', payload, format='json')

    def test_official_sense_is_forked_not_edited(self):
        official = self.sense()
        self.login(self.alice)
        res = self.update(official)
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.data['contents'], dict)  # trước đây gán nguyên tuple (res, missing) nên response trả mảng

        official.refresh_from_db()
        self.assertEqual(official.delta, {})
        fork = AISense.objects.get(id=res.data['id'])
        self.assertNotEqual(fork.id, official.id)
        self.assertEqual((fork.is_official, str(fork.original_id), fork.created_by_id, fork.delta), (False, str(official.id), self.alice.id, {'definition': 'mine'}))
        self.assertIsNone(fork.previous_id)

    def test_own_custom_sense_is_edited_in_place(self):
        mine = self.sense(is_official=False, is_frozen=False, created_by=self.alice, delta={'a': {'x': 1}})
        self.login(self.alice)
        res = self.update(mine, delta={'a': {'y': 2}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(AISense.objects.count(), 1)
        mine.refresh_from_db()
        self.assertEqual(mine.delta, {'a': {'x': 1, 'y': 2}})

    def test_frozen_custom_sense_is_forked_with_previous(self):
        frozen = self.sense(is_official=False, is_frozen=True, created_by=self.alice)
        self.login(self.alice)
        res = self.update(frozen)
        fork = AISense.objects.get(id=res.data['id'])
        self.assertNotEqual(fork.id, frozen.id)
        self.assertEqual((str(fork.previous_id), str(fork.original_id)), (str(frozen.id), str(frozen.id)))

    def test_cannot_touch_someone_elses_custom_sense(self):
        theirs = self.sense(is_official=False, is_frozen=False, created_by=self.bob, delta={'k': 1})
        self.login(self.alice)
        self.assertEqual(self.update(theirs).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.delta, {'k': 1})  # trước đây user bất kỳ sửa tại chỗ được nghĩa tuỳ chỉnh của người khác

    def test_update_metadata_makes_new_row_from_whitelisted_fields(self):
        meta = AISenseMetadata.objects.create(tags=['old'], image_keywords=['kw'])
        official = self.sense(metadata=meta)
        self.login(self.alice)
        res = self.update(official, metadata={'id': 'forged', 'tags': ['new']})
        self.assertEqual(res.status_code, 200)
        fork = AISense.objects.get(id=res.data['id'])
        self.assertNotEqual(fork.metadata_id, meta.id)
        self.assertEqual((fork.metadata.tags, fork.metadata.image_keywords), (['new'], ['kw']))
        meta.refresh_from_db()
        self.assertEqual(meta.tags, ['old'])

    def test_update_metadata_when_sense_has_none(self):
        official = self.sense(metadata=None)
        self.login(self.alice)
        self.assertEqual(self.update(official, metadata={'tags': ['x']}).status_code, 200)  # trước đây AttributeError trên sense.metadata.__dict__

    def test_update_validation(self):
        official = self.sense()
        self.login(self.alice)
        self.assertEqual(self.update(official, sense_id='nope').status_code, 400)
        self.assertEqual(self.update(official, delta=[1]).status_code, 400)
        self.assertEqual(self.update(official, delta={'big': 'x' * 30000}).status_code, 400)
        self.assertEqual(self.update(official, image=5).status_code, 400)
        self.assertEqual(self.update(official, sense_id=str(uuid7())).status_code, 404)
        self.assertEqual(AISense.objects.count(), 1)

    def test_update_requires_login(self):
        self.assertEqual(self.update(self.sense()).status_code, 401)


@override_settings(CACHES=LOCMEM_CACHE)
class GlobalThrottleTests(APITestCase):
    def test_anonymous_requests_are_rate_limited(self):
        django_cache.clear()
        with mock.patch.dict(AnonRateThrottle.THROTTLE_RATES, {'anon': '3/minute'}):
            codes = [self.client.get('/api/languages/').status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])  # trước đây chỉ khai báo mức, chưa bật lớp throttle nào


class PersonalSenseTests(APITestCase):
    """Nghĩa riêng của user: get-word ghép vào, xoá / ẩn, xem nghĩa người khác đã upload."""

    def setUp(self):
        from utils.redis.word_init import WordCacheManager
        self.cache = WordCacheManager()
        self.cache.cache_word_clear_specific('en', 'zzpersonal')
        self.addCleanup(self.cache.cache_word_clear_specific, 'en', 'zzpersonal')
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.word = AIWord.objects.create(value='zzpersonal', language_code='en', status='COMPLETED')
        self.s1 = self.official('First meaning.')
        self.s2 = self.official('Second meaning.')

    def official(self, definition, **extra):
        data = make_sense(str(uuid7()), definition=definition)
        data.update(word_id=self.word.id, word_value='zzpersonal', **extra)
        return AISense.objects.create(**data)

    def custom(self, user, **extra):
        data = dict(word_id=self.word.id, word_value='zzpersonal', language_code='en', created_by=user,
                    is_official=False, is_frozen=False, is_ai_created=False)
        data.update(extra)
        return AISense.objects.create(**data)

    def get_word(self, user=None):
        self.client.force_authenticate(user)
        res = self.client.get('/api/ai-words/get-word/', {'value': 'zzpersonal', 'lang': 'en', 'user_lang': 'vi'})
        self.assertEqual(res.status_code, 200)
        return list(res.data['data']['senses'].items())

    def test_get_word_puts_own_senses_first_replacing_and_hiding(self):
        fork = self.custom(self.alice, original=self.s1, delta={'definition': 'mine'}, contents={})
        new = self.custom(self.alice, contents={'definition': {'en': {'value': 'My own meaning.'}}})
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.post(f'/api/ai-senses/{self.s2.id}/hide/').data['hidden'], True)

        senses = self.get_word(self.alice)
        self.assertEqual([sid for sid, _ in senses], [str(new.id), str(fork.id)])  # s1 bị bản sửa thay, s2 bị ẩn
        fork_data = dict(senses)[str(fork.id)]
        self.assertEqual(fork_data['contents']['definition']['en']['value'], 'First meaning.')  # contents của nghĩa gốc
        self.assertEqual((fork_data['delta'], str(fork_data['original']), fork_data['is_official']), ({'definition': 'mine'}, str(self.s1.id), False))

        # Người khác (lần này đọc từ cache) và người chưa đăng nhập vẫn thấy nghĩa chính thức
        self.assertEqual([sid for sid, _ in self.get_word(self.bob)], [str(self.s1.id), str(self.s2.id)])
        self.assertEqual(len(self.get_word()), 2)
        self.assertEqual(len(self.cache.cache_word_get_data('en', 'zzpersonal')['senses']), 2)  # cache không dính nghĩa riêng

        self.client.force_authenticate(self.alice)
        self.client.delete(f'/api/ai-senses/{self.s2.id}/hide/')
        self.assertIn(str(self.s2.id), dict(self.get_word(self.alice)))

    def test_delete_rules(self):
        from core.models import Collection, CollectionItem
        mine = self.custom(self.alice, contents={'definition': {'en': {'value': 'x'}}})
        frozen = self.custom(self.alice, is_frozen=True)
        theirs = self.custom(self.bob)
        collection = Collection.objects.create(name='c', created_by=self.alice)
        CollectionItem.objects.create(collection=collection, sense=mine, original=mine, created_by=self.alice, released=False)

        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.delete(f'/api/ai-senses/{self.s1.id}/').status_code, 403)
        self.assertEqual(self.client.delete(f'/api/ai-senses/{frozen.id}/').status_code, 409)
        self.assertEqual(self.client.delete(f'/api/ai-senses/{theirs.id}/').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/ai-senses/{mine.id}/').status_code, 204)
        mine.refresh_from_db()
        self.assertTrue(mine.is_deleted)
        self.assertFalse(CollectionItem.objects.filter(sense=mine).exists())  # dòng nháp trỏ tới nghĩa đã xoá bị bỏ
        self.assertEqual(self.client.post(f'/api/ai-senses/{frozen.id}/hide/').status_code, 400)  # nghĩa của mình thì xoá, không ẩn
        self.client.force_authenticate(None)
        self.assertIn(self.client.delete(f'/api/ai-senses/{self.s1.id}/').status_code, (401, 403))

    def test_mine_lists_own_senses_and_hidden(self):
        mine = self.custom(self.alice)
        self.custom(self.bob)
        self.client.force_authenticate(self.alice)
        self.client.post(f'/api/ai-senses/{self.s1.id}/hide/')
        res = self.client.get('/api/ai-senses/mine/', {'word_id': str(self.word.id)})
        self.assertEqual([s['id'] for s in res.data['senses']], [str(mine.id)])
        self.assertEqual(res.data['hidden'], [str(self.s1.id)])

    def test_published_senses_of_others_are_visible(self):
        from core.models import Collection, CollectionItem, UserCollection
        published = self.custom(self.bob, is_frozen=True, contents={'definition': {'en': {'value': 'Bob meaning.'}}})
        private = self.custom(self.bob)
        collection = Collection.objects.create(name='c', created_by=self.bob, released=True, version=1)
        CollectionItem.objects.create(collection=collection, sense=published, original=published, created_by=self.bob, released=True, version=1)

        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.get(f'/api/ai-senses/{published.id}/').status_code, 200)
        self.assertEqual(self.client.get(f'/api/ai-senses/{private.id}/').status_code, 404)

        own = Collection.objects.create(name='mine', created_by=self.alice)
        uc = UserCollection.objects.create(user=self.alice, collection=own, name='mine', created_by=self.alice)
        res = self.client.post(f'/api/user-collections/{uc.id}/add-sense/', {'user_collection_id': str(uc.id), 'sense_id': str(published.id)}, format='json')
        self.assertEqual(res.status_code, 200)
        res = self.client.post(f'/api/user-collections/{uc.id}/add-sense/', {'user_collection_id': str(uc.id), 'sense_id': str(private.id)}, format='json')
        self.assertEqual(res.status_code, 404)
