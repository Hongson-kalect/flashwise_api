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

    def test_edited_official_sense_forks_with_pos_and_shows_delta_in_get_word(self):
        self.client.force_authenticate(self.alice)
        delta = {'definition': {'en': {'value': 'Edited meaning.'}, 'vi': {'value': 'Nghĩa đã sửa.'}}}
        res = self.client.put('/api/ai-senses/update-sense/', {'sense_id': str(self.s1.id), 'user_lang': 'vi', 'delta': delta}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        fork = AISense.objects.get(pk=res.data['id'])
        self.assertNotEqual(fork.pk, self.s1.pk)
        self.assertEqual((fork.pos, fork.level, fork.ipas), (self.s1.pos, self.s1.level, self.s1.ipas))  # không thành tab "Unknown"
        senses = dict(self.get_word(self.alice))
        self.assertNotIn(str(self.s1.id), senses)  # bản tách thay nghĩa gốc
        shown = senses[str(fork.id)]
        self.assertEqual(shown['contents']['definition']['en']['value'], 'Edited meaning.')
        self.assertEqual(shown['contents']['definition']['vi']['value'], 'Nghĩa đã sửa.')
        self.assertEqual(shown['contents']['examples'], self.s1.contents['examples'])  # phần không sửa lấy từ nghĩa gốc
        self.assertEqual(dict(self.get_word(self.bob))[str(self.s1.id)]['contents']['definition']['en']['value'], 'First meaning.')

    def test_editing_japanese_text_regenerates_ruby(self):
        ja = AIWord.objects.create(value='zz猫', language_code='ja', status='COMPLETED')
        base = make_sense(str(uuid7()), definition='x')
        base.update(word_id=ja.id, word_value='zz猫', language_code='ja', contents={'definition': {'ja': {'value': '猫は動物です。', 'roman': 'neko', 'ruby': 'old'}}})
        sense = AISense.objects.create(**base)
        self.client.force_authenticate(self.alice)
        delta = {'definition': {'ja': {'value': '猫は小さい動物です。'}}, 'examples': [{'ja': {'value': '猫が好きです。'}}]}
        res = self.client.put('/api/ai-senses/update-sense/', {'sense_id': str(sense.id), 'user_lang': 'vi', 'delta': delta}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        fork = AISense.objects.get(pk=res.data['id'])
        self.assertIn('[猫|', fork.delta['definition']['ja']['ruby'])
        self.assertNotEqual(fork.delta['definition']['ja']['ruby'], 'old')
        self.assertIn('ruby', fork.delta['examples'][0]['ja'])
        self.assertEqual(fork.delta['definition']['ja']['roman'], 'neko wa chiisai dōbutsu desu.')  # roman cũ ('neko') được sinh lại theo chữ mới

    def test_metadata_only_edit_adds_words_and_shows_in_get_word(self):
        self.client.force_authenticate(self.alice)
        metadata = {'advanced': {'collocations': ['open an account', 'bank balance'], 'synonyms': ['depository']}, 'tags': ['finance', 'money']}
        res = self.client.put('/api/ai-senses/update-sense/', {'sense_id': str(self.s1.id), 'user_lang': 'vi', 'delta': {}, 'metadata': metadata}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        shown = dict(self.get_word(self.alice))[res.data['id']]
        self.assertEqual(shown['metadata']['advanced']['collocations'], ['open an account', 'bank balance'])
        self.assertEqual(shown['metadata']['tags'], ['finance', 'money'])
        self.assertEqual(shown['contents']['definition']['en']['value'], 'First meaning.')  # nội dung giữ nguyên
        self.assertEqual(shown['pos'], self.s1.pos)
        self.assertEqual(self.s1.metadata, AISense.objects.get(pk=self.s1.pk).metadata)  # nghĩa gốc không đổi

    def test_get_word_puts_own_senses_first_replacing_and_hiding(self):
        fork = self.custom(self.alice, original=self.s1, delta={'definition': {'en': {'value': 'Mine.'}}}, contents={})
        new = self.custom(self.alice, contents={'definition': {'en': {'value': 'My own meaning.'}}})
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.post(f'/api/ai-senses/{self.s2.id}/hide/').data['hidden'], True)

        senses = self.get_word(self.alice)
        self.assertEqual([sid for sid, _ in senses], [str(new.id), str(fork.id)])  # s1 bị bản sửa thay, s2 bị ẩn
        fork_data = dict(senses)[str(fork.id)]
        self.assertEqual(fork_data['contents']['definition']['en']['value'], 'Mine.')  # delta đã áp lên contents của nghĩa gốc
        self.assertEqual(fork_data['contents']['examples'], self.s1.contents['examples'])
        self.assertEqual((fork_data['delta'], str(fork_data['original']), fork_data['is_official']), ({'definition': {'en': {'value': 'Mine.'}}}, str(self.s1.id), False))

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


class CommunitySenseApiTests(APITestCase):
    """GET ai-senses/community/: gợi ý nghĩa người khác đã phát hành, xếp theo like / số bản tách / uy tín."""

    def setUp(self):
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.carol = User.objects.create_user('carol')
        self.word = AIWord.objects.create(value='zzcommunity', language_code='en', status='COMPLETED')
        data = make_sense(str(uuid7()), definition='Base meaning.')
        data.update(word_id=self.word.id, word_value='zzcommunity')
        self.base = AISense.objects.create(**data)
        self.collection = None

    def custom(self, user, publish=True, **extra):
        from core.models import Collection, CollectionItem
        data = dict(word_id=self.word.id, word_value='zzcommunity', language_code='en', created_by=user, is_official=False,
                    is_frozen=True, is_ai_created=False, original=self.base, delta={}, contents={})
        data.update(extra)
        sense = AISense.objects.create(**data)
        if publish:
            if self.collection is None:
                self.collection = Collection.objects.create(name='c', created_by=user, released=True, version=1)
            CollectionItem.objects.create(collection=self.collection, sense=sense, original=sense, created_by=user, released=True, version=1)
        return sense

    def get(self, **params):
        params.setdefault('word_id', str(self.word.id))
        return self.client.get('/api/ai-senses/community/', params)

    def test_only_published_frozen_clean_senses_of_others(self):
        shown = self.custom(self.bob, like_count=1)
        self.custom(self.bob, publish=False)                      # chưa phát hành
        self.custom(self.bob, is_frozen=False)                   # chưa đóng băng
        self.custom(self.bob, is_offensive=True)                 # nội dung xấu
        self.custom(self.bob, quality_status='flagged')          # bị đánh dấu
        res = self.get()
        self.assertEqual(res.status_code, 200)
        self.assertEqual([r['id'] for r in res.data['senses']], [str(shown.id)])

    def test_empty_when_nobody_published(self):
        self.assertEqual(self.get().data['senses'], [])

    def test_ranking_by_likes_adoption_reputation_and_verified(self):
        from user.models import UserProfile
        UserProfile.objects.filter(user=self.bob).update(reputation=100)
        liked = self.custom(self.bob, like_count=30)
        adopted = self.custom(self.carol, like_count=5)
        self.custom(self.alice, publish=False, previous=adopted)  # bản tách từ `adopted`
        self.custom(self.alice, publish=False, previous=adopted)
        verified = self.custom(self.carol, quality_status='verified', like_count=0)
        plain = self.custom(self.carol, like_count=0)
        rows = self.get().data['senses']
        order = [r['id'] for r in rows]
        # bob uy tín 100 (+10) + like 30 > carol: like 5 + 2 bản tách > verified (+3) > plain
        self.assertEqual(order, [str(liked.id), str(adopted.id), str(verified.id), str(plain.id)])
        by_id = {r['id']: r for r in rows}
        self.assertEqual(by_id[str(adopted.id)]['adopted_count'], 2)
        self.assertGreater(by_id[str(liked.id)]['rank'], by_id[str(adopted.id)]['rank'])

    def test_newest_ordering_and_limit(self):
        a, b, c = (self.custom(self.bob) for _ in range(3))
        res = self.get(ordering='newest', limit=2)
        self.assertEqual([r['id'] for r in res.data['senses']], [str(c.id), str(b.id)])
        self.assertEqual(res.data['ordering'], 'newest')

    def test_contents_merge_delta_and_keep_requested_languages_only(self):
        self.custom(self.bob, delta={'definition': {'en': {'value': 'Bob wording.'}, 'vi': {'value': 'Cách nói của Bob.'}, 'fr': {'value': 'x'}}})
        row = self.get(user_lang='vi').data['senses'][0]
        self.assertEqual(set(row['contents']['definition']), {'en', 'vi'})
        self.assertEqual(row['contents']['definition']['vi']['value'], 'Cách nói của Bob.')
        # trường delta không đụng tới thì kế thừa từ nghĩa gốc
        self.assertEqual(row['contents']['examples'][0]['en']['value'], 'I went to the bank.')

    def test_authenticated_hides_own_and_hidden_and_marks_liked(self):
        from interact.models import UserInteraction
        mine = self.custom(self.alice)
        hidden = self.custom(self.bob)
        liked = self.custom(self.bob)
        UserInteraction.objects.create(user=self.alice, target_type='sense', target_id=hidden.id, interaction_type='hide', status=True, version=1)
        UserInteraction.objects.create(user=self.alice, target_type='sense', target_id=liked.id, interaction_type='like', status=True, version=1)
        self.client.force_authenticate(self.alice)
        rows = self.get().data['senses']
        self.assertEqual([r['id'] for r in rows], [str(liked.id)])
        self.assertTrue(rows[0]['liked'])
        # khách vẫn thấy cả ba
        self.client.force_authenticate(None)
        self.assertEqual(len(self.get().data['senses']), 3)

    def test_author_info_without_private_fields(self):
        from user.models import UserProfile
        UserProfile.objects.filter(user=self.bob).update(full_name='Bob', reputation=40)
        self.custom(self.bob)
        author = self.get().data['senses'][0]['author']
        self.assertEqual(author['name'], 'Bob')
        self.assertEqual(author['reputation'], 40)
        self.assertEqual(set(author), {'id', 'name', 'reputation'})  # không lộ user id / email

    def test_validation(self):
        self.assertEqual(self.client.get('/api/ai-senses/community/').status_code, 400)
        self.assertEqual(self.get(word_id='nope').status_code, 400)
        self.assertEqual(self.get(ordering='random').status_code, 400)
        self.assertEqual(self.get(user_lang='english').status_code, 400)
        self.assertEqual(self.get(limit='x').status_code, 400)
        self.assertEqual(self.get(word_id=str(uuid7())).status_code, 404)


def _claude_result(verdict='accept', confidence=0.9, **over):
    """Kết quả giả của utils.ai.sense_gate.propose_claude."""
    data = {
        'verdict': verdict, 'reason': 'Looks fine.', 'duplicate_of': 0, 'pos_correct': True, 'suggested_pos': 'noun',
        'is_accurate': True, 'is_offensive_or_spam': False, 'injection_attempt': False, 'confidence': confidence,
        'sense': {
            'pos': 'noun', 'definition': {'value': 'A supply kept for future use.'}, 'usage': {'value': 'Often in compounds.'},
            'examples': [{'value': 'The blood bank was full.'}, {'value': 'A data bank grew.'}], 'level': 'B2', 'register': ['neutral'],
            'ipas': [{'label': 'US', 'value': '/bæŋk/'}], 'is_offensive': False,
            'translation': {
                'definition': 'Nguồn dự trữ để dùng sau này.', 'usage': 'Thường dùng trong từ ghép.',
                'examples': ['Ngân hàng máu đã đầy.', 'Kho dữ liệu lớn dần.'], 'translations': ['kho dự trữ'],
            },
            'metadata': {'tags': ['storage'], 'image_keywords': ['blood bag'], 'synonyms': ['store'], 'antonyms': [], 'collocations': ['blood bank']},
        },
        'model': 'fake-claude', 'seconds': 0.1, 'attempts': 1, 'translation_problem': None, 'tokens': {'in': 1, 'out': 1},
    }
    data.update(over)
    return data


def _gemini_result(verdict='accept', **over):
    data = {
        'verdict': verdict, 'reason': 'ok', 'duplicate_of': 0, 'pos_correct': True, 'suggested_pos': 'noun', 'is_accurate': True,
        'is_offensive_or_spam': False, 'injection_attempt': False, 'confidence': 1.0, 'model': 'fake-gemini', 'seconds': 0.1,
        'tokens': {'in': 1, 'out': 1},
    }
    data.update(over)
    return data


@override_settings(CACHES=LOCMEM_CACHE)
class ProposeSenseApiTests(APITestCase):
    """POST ai-senses/propose: Claude chấm + viết, Gemini kiểm chéo song song, không chờ Gemini nếu Claude xong trước."""

    def setUp(self):
        import asyncio
        django_cache.clear()
        from user.models import UserProfile
        self.member = User.objects.create_user('member')
        self.other = User.objects.create_user('other')
        self.guest = User.objects.create_user('guest-user')
        self.admin = User.objects.create_user('admin-user', is_staff=True)
        UserProfile.objects.filter(user__in=[self.member, self.other]).update(tier='normal_user')
        self.word = AIWord.objects.create(value='zzpropose', language_code='en', status='COMPLETED')
        data = make_sense(str(uuid7()), definition='A financial institution.')
        data.update(word_id=self.word.id, word_value='zzpropose')
        self.base = AISense.objects.create(**data)
        self.asyncio = asyncio
        self.client.force_authenticate(self.member)

    def run_propose(self, claude=None, gemini=None, claude_error=None, gemini_error=False, gemini_delay=0, definition='A stock of something kept for later use.', **body):
        asyncio = self.asyncio

        async def fake_claude(prompt, language_code, user_language_code, **kwargs):
            if claude_error:
                raise claude_error
            return claude or _claude_result()

        async def fake_gemini(prompt, **kwargs):
            if gemini_delay:
                await asyncio.sleep(gemini_delay)
            if gemini_error:
                raise RuntimeError('gemini down')
            return gemini or _gemini_result()

        payload = {'word_id': str(self.word.id), 'user_lang': 'vi', 'definition': definition, 'pos': 'noun', **body}
        with mock.patch('utils.ai.sense_gate.propose_claude', fake_claude), mock.patch('utils.ai.sense_gate.gate_gemini', fake_gemini):
            return self.client.post('/api/ai-senses/propose/', payload, format='json')

    def proposed(self):
        return AISense.objects.filter(word=self.word).exclude(pk=self.base.pk)

    # ---------- chính thức ----------

    def test_both_accept_creates_official_sense_and_rewards_author(self):
        from user.models import ReputationEvent
        res = self.run_propose()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['result'], 'official')
        sense = AISense.objects.get(pk=res.data['sense']['id'])
        self.assertTrue(sense.is_official)
        self.assertTrue(sense.is_frozen)
        self.assertEqual(sense.created_by, self.member)
        self.assertEqual(sense.ai_review['proposal']['status'], 'approved')
        self.assertEqual(sense.contents['definition']['vi']['value'], 'Nguồn dự trữ để dùng sau này.')
        self.assertEqual(sense.contents['translations']['vi'], ['kho dự trữ'])
        self.assertEqual(len(sense.contents['examples']), 2)
        self.assertEqual(sense.metadata.tags, ['storage'])
        self.assertTrue(ReputationEvent.objects.filter(user=self.member, kind='sense_accepted').exists())
        # ai ra cũng thấy nghĩa chính thức
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f'/api/ai-senses/{sense.id}/').status_code, 200)

    # ---------- chờ admin duyệt ----------

    def assert_pending(self, res, reason):
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['result'], 'pending')
        self.assertIn(reason, res.data['reasons'])
        sense = AISense.objects.get(pk=res.data['sense']['id'])
        self.assertFalse(sense.is_official)
        self.assertEqual(sense.ai_review['proposal']['status'], 'pending')
        self.assertEqual(sense.created_by, self.member)
        return sense

    def test_gemini_not_back_when_claude_finishes_means_pending(self):
        sense = self.assert_pending(self.run_propose(gemini_delay=0.4), 'no_second_opinion')
        self.assertEqual(sense.ai_review['proposal']['gemini_status'], 'pending')
        self.assertIsNone(sense.ai_review['proposal']['gemini'])

    def test_gemini_error_means_pending(self):
        sense = self.assert_pending(self.run_propose(gemini_error=True), 'no_second_opinion')
        self.assertEqual(sense.ai_review['proposal']['gemini_status'], 'error')

    def test_low_confidence_means_pending(self):
        self.assert_pending(self.run_propose(claude=_claude_result(confidence=0.7)), 'low_confidence')

    def test_confidence_at_threshold_is_enough(self):
        self.assertEqual(self.run_propose(claude=_claude_result(confidence=0.8)).data['result'], 'official')

    def test_gemini_disagreement_means_pending(self):
        self.assert_pending(self.run_propose(gemini=_gemini_result('uncertain')), 'gemini_uncertain')
        self.assert_pending(self.run_propose(gemini=_gemini_result('reject')), 'gemini_reject')

    def test_claude_uncertain_means_pending(self):
        self.assert_pending(self.run_propose(claude=_claude_result('uncertain', 0.9)), 'claude_uncertain')

    def test_broken_translation_means_pending(self):
        self.assert_pending(self.run_propose(claude=_claude_result(translation_problem='không phải tiếng Việt')), 'translation_problem')

    def test_pending_sense_is_private_to_author_until_approved(self):
        sense = self.assert_pending(self.run_propose(gemini_error=True), 'no_second_opinion')
        # kể cả khi tác giả phát hành bộ chứa nghĩa này, người khác vẫn không thấy
        from core.models import Collection, CollectionItem
        collection = Collection.objects.create(name='c', created_by=self.member, released=True, version=1)
        CollectionItem.objects.create(collection=collection, sense=sense, original=sense, created_by=self.member, released=True, version=1)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f'/api/ai-senses/{sense.id}/').status_code, 404)
        self.assertEqual(self.client.get('/api/ai-senses/community/', {'word_id': str(self.word.id)}).data['senses'], [])
        # tác giả vẫn thấy
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(f'/api/ai-senses/{sense.id}/').status_code, 200)

    def test_admin_queue_and_promote_approves(self):
        sense = self.assert_pending(self.run_propose(gemini_error=True), 'no_second_opinion')
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get('/api/ai-senses/proposals/').status_code, 403)
        self.client.force_authenticate(self.admin)
        rows = self.client.get('/api/ai-senses/proposals/').data['senses']
        self.assertEqual([r['id'] for r in rows], [str(sense.id)])
        self.assertEqual(rows[0]['proposal']['reasons'], ['no_second_opinion'])
        self.assertEqual(self.client.get('/api/ai-senses/proposals/', {'status': 'zzz'}).status_code, 400)
        self.assertEqual(self.client.post(f'/api/ai-senses/{sense.id}/promote/', {}, format='json').status_code, 200)
        sense.refresh_from_db()
        self.assertTrue(sense.is_official)
        self.assertEqual(sense.ai_review['proposal']['status'], 'approved')
        self.assertEqual(self.client.get('/api/ai-senses/proposals/').data['senses'], [])
        self.assertEqual(len(self.client.get('/api/ai-senses/proposals/', {'status': 'approved'}).data['senses']), 1)

    # ---------- trùng / từ chối ----------

    def test_duplicate_returns_existing_sense_and_creates_nothing(self):
        res = self.run_propose(claude=_claude_result('duplicate', 0.95, duplicate_of=1), gemini=_gemini_result('duplicate'))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['result'], 'duplicate')
        self.assertEqual(res.data['sense']['id'], str(self.base.id))
        self.assertFalse(self.proposed().exists())

    def test_reject_creates_nothing(self):
        res = self.run_propose(claude=_claude_result('reject', 0.9), gemini=_gemini_result('reject'))
        self.assertEqual(res.data['result'], 'rejected')
        self.assertFalse(res.data['abuse'])
        self.assertEqual(res.data['reason'], 'Looks fine.')
        self.assertFalse(self.proposed().exists())

    def test_abuse_is_rejected_and_costs_reputation(self):
        from user.models import ReputationEvent
        res = self.run_propose(claude=_claude_result('reject', 1.0, injection_attempt=True))
        self.assertTrue(res.data['abuse'])
        self.assertTrue(ReputationEvent.objects.filter(user=self.member, kind='sense_abuse').exists())
        self.assertFalse(self.proposed().exists())

    def test_claude_reject_stands_even_if_gemini_accepts(self):
        # Claude không viết nội dung khi reject nên không có gì để chờ duyệt; Claude chặt hơn Gemini nhỏ
        res = self.run_propose(claude=_claude_result('reject', 0.9), gemini=_gemini_result('accept'))
        self.assertEqual(res.data['result'], 'rejected')
        self.assertFalse(self.proposed().exists())

    # ---------- kiểm tra đầu vào / quyền ----------

    def test_validation(self):
        self.assertEqual(self.run_propose(definition='short').status_code, 400)
        self.assertEqual(self.run_propose(definition='x' * 501).status_code, 400)
        self.assertEqual(self.run_propose(definition=123).status_code, 400)
        self.assertEqual(self.run_propose(word_id='nope').status_code, 400)
        self.assertEqual(self.run_propose(user_lang='english').status_code, 400)
        self.assertEqual(self.run_propose(word_id=str(uuid7())).status_code, 404)
        self.assertFalse(self.proposed().exists())

    def test_japanese_sense_gets_computed_roman_and_ruby(self):
        ja = AIWord.objects.create(value='zz犬', language_code='ja', status='COMPLETED')
        sense = _claude_result()['sense']
        sense.update(
            pos='noun', definition={'value': '警察に情報を売る人。', 'roman': 'keisatsu ni jōhō o uru hito.'},
            usage={'value': ''}, level='N1', ipas=[{'label': 'READING', 'value': 'いぬ', 'roman': 'inu'}],
            examples=[{'value': '彼は犬だと疑われた。', 'roman': 'kare wa inu da to utagawareta.'}, {'value': '犬を使って探った。', 'roman': 'inu o tsukatte sagutta.'}],
        )
        res = self.run_propose(claude=_claude_result(sense=sense), word_id=str(ja.id), pos='noun')
        self.assertEqual(res.status_code, 201, res.data)
        contents = AISense.objects.get(pk=res.data['sense']['id']).contents
        self.assertEqual(contents['definition']['ja']['roman'], 'keisatsu ni jōhō o uru hito.')  # server sinh bằng fugashi, bỏ roman do AI gửi
        self.assertIn('[', contents['definition']['ja']['ruby'])  # furigana do fugashi sinh
        self.assertEqual(contents['examples'][0]['ja']['roman'], 'kare wa inu da to utagawareta.')
        self.assertEqual(contents['definition']['vi']['value'], 'Nguồn dự trữ để dùng sau này.')

    def test_roman_languages_schema_has_no_roman(self):
        from utils.ai import sense_gate
        sense = sense_gate.PROPOSE_SCHEMA_JA['properties']['sense']['properties']
        self.assertNotIn('roman', sense['definition']['properties'])  # roman do server sinh (utils/ai/romanize.py)
        self.assertNotIn('roman', sense['examples']['items']['properties'])
        self.assertNotIn('roman', sense['ipas']['items']['properties'])
        self.assertNotIn('roman', sense_gate.PROPOSE_SCHEMA['properties']['sense']['properties']['definition']['properties'])

    def test_guest_needs_an_account_unless_allowed(self):
        self.client.force_authenticate(self.guest)
        with override_settings(SENSE_PROPOSE_ALLOW_GUEST=False):  # không phụ thuộc .env dev
            res = self.run_propose()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['errorCode'], 'account_required')
        with override_settings(SENSE_PROPOSE_ALLOW_GUEST=True):
            self.assertEqual(self.run_propose().status_code, 201)

    def test_login_required(self):
        self.client.force_authenticate(None)
        self.assertIn(self.run_propose().status_code, (401, 403))

    def test_claude_failure_is_503_and_creates_nothing(self):
        res = self.run_propose(claude_error=RuntimeError('boom'))
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data['errorCode'], 'ai_unavailable')
        self.assertFalse(self.proposed().exists())

    def test_existing_senses_are_sent_even_when_pos_is_omitted(self):
        """Không chọn loại từ thì phải gửi mọi nghĩa đang có, nếu không AI không có gì để so trùng (lỗi từng gặp khi thử UI)."""
        prompts = []

        async def fake_claude(prompt, language_code, user_language_code, **kwargs):
            prompts.append(prompt)
            return _claude_result('duplicate', 0.95, duplicate_of=1)

        async def fake_gemini(prompt, **kwargs):
            prompts.append(prompt)
            return _gemini_result('duplicate')

        payload = {'word_id': str(self.word.id), 'user_lang': 'vi', 'definition': 'A place where money is kept.'}
        for pos in ('', 'noun'):
            prompts.clear()
            with mock.patch('utils.ai.sense_gate.propose_claude', fake_claude), mock.patch('utils.ai.sense_gate.gate_gemini', fake_gemini):
                res = self.client.post('/api/ai-senses/propose/', {**payload, 'pos': pos}, format='json')
            self.assertEqual(res.data['result'], 'duplicate')
            self.assertEqual(len(prompts), 2)
            for prompt in prompts:
                self.assertIn('A financial institution.', prompt)

    def test_throttle(self):
        from rest_framework.throttling import SimpleRateThrottle
        from utils.utils.permissions import SenseProposeThrottle
        rates = {**SimpleRateThrottle.THROTTLE_RATES, 'sense_propose': '2/hour'}
        with mock.patch.object(SenseProposeThrottle, 'THROTTLE_RATES', rates):
            codes = [self.run_propose(claude=_claude_result('reject')).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
