from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase
from uuid6 import uuid7

from ai.models import AISense, AIWord
from ai.tests import FakeQueue
from core.models import Collection, CollectionItem, UserCollection
from utils.redis.word_init import WordCacheManager
from utils.utils.permissions import consume_daily_quota

User = get_user_model()

LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class FakeRedis:
    def __init__(self):
        self.sets = {}

    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)


def make_word_with_sense(value, language='en', score=0, **sense_kwargs):
    word = AIWord.objects.create(value=value, language_code=language, status='COMPLETED')
    sense = AISense.objects.create(
        word=word, word_value=value, language_code=language, pos='noun', score=score,
        contents={'definition': {language: {'value': f'def of {value}'}}}, **sense_kwargs,
    )
    return word, sense


@override_settings(CACHES=LOCMEM_CACHE)
class CollectionApiTests(APITestCase):
    def setUp(self):
        django_cache.clear()
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.admin = User.objects.create_user('admin-user', is_staff=True)

    def collection(self, name='C', owner=None, **kwargs):
        return Collection.objects.create(name=name, language_code='en', created_by=owner, **kwargs)

    # ---------- xem ----------

    def test_list_shows_official_released_and_own_only(self):
        official = self.collection('official', is_official=True)
        released = self.collection('released', owner=self.bob, released=True)
        mine = self.collection('mine', owner=self.alice)
        theirs = self.collection('theirs', owner=self.bob)

        self.client.force_authenticate(self.alice)
        ids = {row['id'] for row in self.client.get('/api/collections/').data['results']}
        self.assertEqual(ids, {str(official.id), str(released.id), str(mine.id)})

        self.client.force_authenticate(None)
        ids = {row['id'] for row in self.client.get('/api/collections/').data['results']}
        self.assertEqual(ids, {str(official.id), str(released.id)})

        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get('/api/collections/').data['count'], 4)

    def test_list_count_ignores_deleted_items(self):
        collection = self.collection(is_official=True)
        _, sense1 = make_word_with_sense('one')
        _, sense2 = make_word_with_sense('two')
        CollectionItem.objects.create(collection=collection, sense=sense1)
        CollectionItem.objects.create(collection=collection, sense=sense2, is_deleted=True)
        row = self.client.get('/api/collections/').data['results'][0]
        self.assertEqual(row['senses_count'], 1)

    def test_other_users_personal_collection_is_hidden(self):
        theirs = self.collection(owner=self.bob)
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.get(f'/api/collections/{theirs.id}/').status_code, 404)
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.get(f'/api/collections/{theirs.id}/').status_code, 200)

    def test_detail_follows_display_order_and_skips_deleted_items(self):
        collection = self.collection(owner=self.alice)
        senses = [make_word_with_sense(w)[1] for w in ('alpha', 'beta', 'gamma')]
        CollectionItem.objects.create(collection=collection, sense=senses[0], display_order=2)
        CollectionItem.objects.create(collection=collection, sense=senses[1], display_order=0)
        CollectionItem.objects.create(collection=collection, sense=senses[2], display_order=1, is_deleted=True)

        self.client.force_authenticate(self.alice)
        res = self.client.get(f'/api/collections/{collection.id}/')
        self.assertEqual([s['word_value'] for s in res.data['senses']], ['beta', 'alpha'])  # trước đây theo thứ tự của bảng nghĩa (mới nhất trước), mất thứ tự người dùng sắp

    # ---------- từ đang chờ ----------

    def test_pending_words_are_resolved_on_view(self):
        word, sense = make_word_with_sense('zzdone', score=5)
        AISense.objects.create(word=word, word_value='zzdone', language_code='en', score=9, is_official=False, created_by=self.bob, contents={})  # nghĩa tuỳ chỉnh điểm cao hơn không được chọn
        AIWord.objects.create(value='zzdone', language_code='ja', status='COMPLETED')    # cùng chữ nhưng ngôn ngữ khác
        AIWord.objects.create(value='zzbad', language_code='en', status='REJECTED')
        AIWord.objects.create(value='zzwait', language_code='en', status='PROCESSING')
        collection = self.collection(owner=self.alice, pending_words=['zzdone', 'zzbad', 'zzwait'])

        self.client.force_authenticate(self.alice)
        res = self.client.get(f'/api/collections/{collection.id}/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual([s['id'] for s in res.data['senses']], [str(sense.id)])
        self.assertEqual(res.data['pending_words'], ['zzwait'])
        self.assertEqual(res.data['invalid_words'], ['zzbad'])  # REJECTED mới là trạng thái thật, code cũ chỉ biết 'INVALID' nên từ bị từ chối chờ mãi
        self.assertEqual(res.data['item_count'], 1)

        again = self.client.get(f'/api/collections/{collection.id}/')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(CollectionItem.objects.filter(collection=collection).count(), 1)  # xem lại không thêm trùng

    def test_pending_word_is_added_after_it_completes(self):
        collection = self.collection(owner=self.alice, pending_words=['zzlater'])
        word = AIWord.objects.create(value='zzlater', language_code='en', status='PROCESSING')
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.get(f'/api/collections/{collection.id}/').data['pending_words'], ['zzlater'])

        word.status = 'COMPLETED'
        word.save()
        AISense.objects.create(word=word, word_value='zzlater', language_code='en', contents={})
        res = self.client.get(f'/api/collections/{collection.id}/')
        self.assertEqual((res.data['pending_words'], len(res.data['senses'])), ([], 1))

    # ---------- sửa / xoá ----------

    def test_owner_cannot_escalate_via_update(self):
        collection = self.collection('Old', owner=self.alice)
        self.client.force_authenticate(self.alice)
        res = self.client.patch(f'/api/collections/{collection.id}/', {'name': 'New', 'is_official': True, 'like_count': 99999, 'released': True, 'version': 7}, format='json')
        self.assertEqual(res.status_code, 200)
        collection.refresh_from_db()
        self.assertEqual(collection.name, 'New')
        self.assertEqual((collection.is_official, collection.like_count, collection.released, collection.version), (False, 0, False, 0))

    def test_only_owner_or_admin_can_modify(self):
        public = self.collection('Public', owner=self.bob, released=True)
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.patch(f'/api/collections/{public.id}/', {'name': 'x'}, format='json').status_code, 403)
        self.assertEqual(self.client.delete(f'/api/collections/{public.id}/').status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f'/api/collections/{public.id}/').status_code, 204)
        public.refresh_from_db()
        self.assertTrue(public.is_deleted)

    # ---------- tạo từ nghĩa có sẵn ----------

    def post_create(self, **body):
        payload = {'name': 'My list', 'description': 'd', 'language_code': 'en', 'senses': []}
        payload.update(body)
        return self.client.post('/api/collections/', payload, format='json')

    def test_create_from_senses(self):
        _, first = make_word_with_sense('first')
        _, second = make_word_with_sense('second')
        self.client.force_authenticate(self.alice)
        res = self.post_create(senses=[{'id': str(second.id)}, {'id': str(first.id)}, {'id': str(second.id)}])
        self.assertEqual(res.status_code, 201)  # trước đây luôn lỗi: UserCollection(...).save() trả None rồi lấy .id
        collection = Collection.objects.get(id=res.data['id'])
        self.assertEqual((collection.created_by_id, collection.item_count), (self.alice.id, 2))
        items = list(collection.collectionitem_set.order_by('display_order'))
        self.assertEqual([str(i.sense_id) for i in items], [str(second.id), str(first.id)])
        self.assertEqual(items[0].original_id, second.id)  # nghĩa chính thức là gốc của chính nó
        self.assertEqual(str(UserCollection.objects.get(collection=collection, user=self.alice).id), str(res.data['user_collection']))

    def test_create_validates_senses_and_ignores_forbidden_fields(self):
        _, official = make_word_with_sense('ok')
        _, private = make_word_with_sense('private', is_official=False, created_by=self.bob)
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.post_create(senses=[{'id': 'nope'}]).status_code, 400)
        self.assertEqual(self.post_create(senses='x').status_code, 400)
        self.assertEqual(self.post_create(senses=[{'id': str(uuid7())}]).status_code, 400)
        self.assertEqual(self.post_create(senses=[{'id': str(private.id)}]).status_code, 400)  # nghĩa riêng của người khác
        self.assertEqual(Collection.objects.count(), 0)

        res = self.post_create(senses=[{'id': str(official.id)}], is_official=True, like_count=500, released=True)
        self.assertEqual(res.status_code, 201)
        collection = Collection.objects.get(id=res.data['id'])
        self.assertEqual((collection.is_official, collection.like_count, collection.released), (False, 0, False))

    def test_create_requires_login(self):
        self.assertEqual(self.post_create().status_code, 401)


@override_settings(CACHES=LOCMEM_CACHE)
class BulkCreateTests(APITestCase):
    def setUp(self):
        django_cache.clear()
        self.alice = User.objects.create_user('alice')
        self.client.force_authenticate(self.alice)
        self.queue = FakeQueue()
        self.redis = FakeRedis()
        for target, replacement in (
            ('ai.generation.push_job', self.queue.push_job),
            ('core.views.Collection.get_queue_client', lambda: self.redis),
        ):
            patcher = mock.patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cache = WordCacheManager()
        self.words = ['zzhave', 'zzreject', 'zznewone', 'zznewtwo', 'zzfailed']
        for word in self.words:
            self.addCleanup(self.cache.cache_word_clear_specific, 'en', word)

    def bulk(self, **body):
        payload = {'name': 'Bulk', 'description': '', 'lang': 'en', 'user_lang': 'vi', 'words': ['zzhave']}
        payload.update(body)
        return self.client.post('/api/collections/bulk-create/', payload, format='json')

    def test_sorts_words_into_items_invalid_and_pending(self):
        _, sense = make_word_with_sense('zzhave')
        AIWord.objects.create(value='zzreject', language_code='en', status='REJECTED')
        failed = AIWord.objects.create(value='zzfailed', language_code='en', status='FAILED')

        res = self.bulk(words=['ZZHave', 'zzhave', 'zzreject', 'zznewone', 'zznewtwo', 'zzfailed', '  ', 5])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['invalid_words'], ['zzreject'])
        self.assertEqual(res.data['pending_words'], ['zznewone', 'zznewtwo', 'zzfailed'])
        self.assertEqual(res.data['item_count'], 1)

        collection = Collection.objects.get(id=res.data['id'])
        self.assertEqual((collection.created_by_id, collection.is_official), (self.alice.id, False))
        self.assertEqual([str(i.sense_id) for i in collection.collectionitem_set.all()], [str(sense.id)])

        # Chỉ từ cần AI mới sinh job, vào đúng hàng đợi của bộ từ; từ FAILED dùng lại bản ghi cũ
        self.assertEqual(self.queue.names(), ['redis_collection_word'] * 3)
        self.assertEqual({job['value'] for _, job in self.queue.jobs}, {'zznewone', 'zznewtwo', 'zzfailed'})
        self.assertEqual(AIWord.objects.filter(value='zzfailed').count(), 1)
        failed.refresh_from_db()
        self.assertEqual(failed.status, 'PROCESSING')
        self.assertEqual(AIWord.objects.get(value='zznewone').status, 'PROCESSING')
        self.assertEqual(self.redis.sets['collection_id:zznewone'], {str(collection.id)})

    def test_words_already_processing_by_others_stay_pending_without_new_job(self):
        AIWord.objects.create(value='zznewone', language_code='en', status='PROCESSING')  # trước đây bị loại khỏi pending và mất hẳn
        res = self.bulk(words=['zznewone'])
        self.assertEqual(res.data['pending_words'], ['zznewone'])
        self.assertEqual(self.queue.jobs, [])

    def test_validation(self):
        self.assertEqual(self.bulk(name='').status_code, 400)
        self.assertEqual(self.bulk(name='x' * 300).status_code, 400)
        self.assertEqual(self.bulk(lang='english').status_code, 400)
        self.assertEqual(self.bulk(words='nope').status_code, 400)
        self.assertEqual(self.bulk(words=[' ', '!!!']).status_code, 400)
        self.assertEqual(self.bulk(words=[f'w{i}' for i in range(201)]).status_code, 400)
        self.assertEqual(self.bulk(**{'user_lang': None}).status_code, 400)
        self.assertEqual(Collection.objects.count(), 0)

    @override_settings(BULK_WORDS_PER_DAY=2)
    def test_daily_quota_blocks_before_any_write(self):
        res = self.bulk(words=['zznewone', 'zznewtwo', 'zzfailed'])
        self.assertEqual(res.status_code, 429)
        self.assertEqual((Collection.objects.count(), AIWord.objects.count(), self.queue.jobs), (0, 0, []))

        self.assertEqual(self.bulk(words=['zznewone', 'zznewtwo']).status_code, 201)  # vừa đủ hạn mức
        self.assertEqual(self.bulk(words=['zzfailed']).status_code, 429)

    def test_generation_failure_keeps_word_pending_and_still_creates_collection(self):
        def boom(queue, payload):
            raise ConnectionError('redis down')

        with mock.patch('ai.generation.push_job', boom):
            res = self.bulk(words=['zznewone'])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['pending_words'], ['zznewone'])
        self.assertEqual(AIWord.objects.get(value='zznewone').status, 'FAILED')  # lần sau tạo lại được


class QuotaTests(TestCase):
    @override_settings(CACHES=LOCMEM_CACHE)
    def test_consume_daily_quota(self):
        django_cache.clear()
        user = User.objects.create_user('quota-user')
        self.assertTrue(consume_daily_quota(user, 'x', 3, 5))
        self.assertFalse(consume_daily_quota(user, 'x', 3, 5))  # vượt thì không cộng
        self.assertTrue(consume_daily_quota(user, 'x', 2, 5))
        self.assertFalse(consume_daily_quota(user, 'x', 1, 5))
        other = User.objects.create_user('other-user')
        self.assertTrue(consume_daily_quota(other, 'x', 5, 5))


class NormalizeImportTests(TestCase):
    def test_still_importable_from_old_location(self):
        from core.views.Collection import normalize as old
        from utils.utils.normalize import normalize
        self.assertIs(old, normalize)
        self.assertEqual(normalize('  Hello,  World! '), 'hello, world')
