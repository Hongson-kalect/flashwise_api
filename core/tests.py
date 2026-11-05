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

    def test_list_count_is_released_word_count(self):
        # Số từ = item_count của bản đang hiệu lực, không đếm nháp / dòng xoá / version cũ trong bảng trung gian
        collection = self.collection(is_official=True, item_count=1)
        _, sense1 = make_word_with_sense('one')
        _, sense2 = make_word_with_sense('two')
        CollectionItem.objects.create(collection=collection, sense=sense1, original=sense1, released=True, version=1)
        CollectionItem.objects.create(collection=collection, sense=sense2, original=sense2, created_by=self.bob)
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
        for sense, order, deleted in zip(senses, (2, 0, 1), (False, False, True)):
            CollectionItem.objects.create(collection=collection, sense=sense, original=sense, display_order=order, is_deleted=deleted, created_by=self.alice)

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
            ('ai.bulk.get_queue_client', lambda: self.redis),
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

        res = self.bulk(words=['ZZHave', ' zzhave ', 'zzreject', 'zznewone', 'zznewtwo', 'zzfailed', '  ', 5])
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

    def test_words_keep_symbols_like_get_word(self):
        self.addCleanup(self.cache.cache_word_clear_specific, 'en', 'zzc++')
        self.addCleanup(self.cache.cache_word_clear_specific, 'en', 'zzice cream')
        res = self.bulk(words=['ZZC++', 'zzc++', '  zzIce   Cream ', '!!!'])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['pending_words'], ['zzc++', 'zzice cream'])  # '!!!' bị bỏ, không gọi AI
        self.assertEqual({job['value'] for _, job in self.queue.jobs}, {'zzc++', 'zzice cream'})
        self.assertEqual(self.cache.cache_word_get_data('en', 'zzc++')['status'], 'PROCESSING')  # get-word tra 'zzc++' sẽ vào chờ chung

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


@override_settings(CACHES=LOCMEM_CACHE)
class UserCollectionApiTests(APITestCase):
    def setUp(self):
        django_cache.clear()
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.client.force_authenticate(self.alice)

    def item(self, collection, sense, **kwargs):
        return CollectionItem.objects.create(collection=collection, sense=sense, original=sense, **kwargs)

    def upload(self, user_collection, **body):
        return self.client.post(f'/api/user-collections/{user_collection.id}/upload/', {'user_collection_id': str(user_collection.id), **body}, format='json')

    def test_user_collections_are_private(self):
        collection = Collection.objects.create(name='Shared', language_code='en', is_official=True)
        theirs = UserCollection.objects.create(user=self.bob, collection=collection)
        self.assertEqual(self.client.get('/api/user-collections/').data['count'], 0)
        self.assertEqual(self.client.get(f'/api/user-collections/{theirs.id}/').status_code, 404)
        self.assertEqual(self.upload(theirs).status_code, 404)

    def test_create_points_to_collections_api(self):
        res = self.client.post('/api/user-collections/', {'name': 'x'}, format='json')
        self.assertEqual(res.status_code, 405)  # trước đây luôn 500: data.created_by = ... trên dict
        self.assertIn('/api/collections/', res.data['message'])

    def test_owner_upload_releases_drafts_as_next_version(self):
        collection = Collection.objects.create(name='Mine', language_code='en', created_by=self.alice, version=1, description='d', metadatas=[{'version': 1}])
        (_, one), (_, two) = make_word_with_sense('one'), make_word_with_sense('two')
        self.item(collection, one, released=True, version=1, created_by=self.alice)
        draft = self.item(collection, two, released=False, created_by=self.alice)
        user_collection = UserCollection.objects.create(user=self.alice, collection=collection, version=1, created_by=self.alice)

        res = self.upload(user_collection, title='v2')
        self.assertEqual(res.status_code, 200)  # trước đây NotSupportedError (FOR UPDATE trên join nullable), rồi lỗi tên trường
        self.assertEqual(res.data['current_version'], 2)

        collection.refresh_from_db()
        self.assertEqual(collection.version, 2)
        self.assertEqual(collection.metadatas[-1]['changelog'], {'added': [str(two.id)], 'updated': [], 'deleted': []})
        self.assertEqual(collection.metadatas[-1]['title'], 'v2')
        draft.refresh_from_db()
        self.assertEqual((draft.released, draft.version), (True, 2))
        user_collection.refresh_from_db()
        self.assertEqual(user_collection.version, 2)

    def test_non_owner_upload_remixes_into_new_collection(self):
        shared = Collection.objects.create(name='Shared', language_code='en', created_by=self.bob, released=True, version=1)
        (_, one), (_, three) = make_word_with_sense('one'), make_word_with_sense('three')
        self.item(shared, one, released=True, version=1, display_order=0)
        draft = self.item(shared, three, released=False, created_by=self.alice, display_order=1)
        user_collection = UserCollection.objects.create(user=self.alice, collection=shared, version=1, created_by=self.alice)

        res = self.upload(user_collection)
        self.assertEqual(res.status_code, 200)

        remix = Collection.objects.get(id=res.data['collection_id'])
        self.assertNotEqual(remix.id, shared.id)
        self.assertEqual((remix.created_by_id, remix.name, remix.version, remix.item_count, remix.language_code), (self.alice.id, 'Shared - Remixed by alice', 1, 2, 'en'))
        self.assertEqual([str(p) for p in remix.parents], [str(shared.id)])
        self.assertEqual(str(remix.previous_id), str(shared.id))
        self.assertEqual({str(i.sense_id) for i in remix.collectionitem_set.all()}, {str(one.id), str(three.id)})

        # Bản nháp rời bộ gốc, bộ gốc của Bob không bị đụng tới
        self.assertFalse(CollectionItem.objects.filter(id=draft.id).exists())
        shared.refresh_from_db()
        self.assertEqual((shared.version, shared.collectionitem_set.count()), (1, 1))
        user_collection.refresh_from_db()
        self.assertEqual((str(user_collection.collection_id), user_collection.version), (str(remix.id), 1))


@override_settings(CACHES=LOCMEM_CACHE)
class UserCollectionEditFlowTests(APITestCase):
    """Luồng: tạo bộ từ nghĩa có sẵn -> sửa / xoá / thêm nghĩa -> phát hành (upload) -> người khác tải về."""

    def setUp(self):
        django_cache.clear()
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.client.force_authenticate(self.alice)
        (_, self.one), (_, self.two), (_, self.three) = [make_word_with_sense(w, is_official=True) for w in ('one', 'two', 'three')]

    def create_collection(self, *senses):
        res = self.client.post('/api/collections/', {'name': 'Mine', 'language_code': 'en', 'senses': [{'id': str(s.id)} for s in senses]}, format='json')
        self.assertEqual(res.status_code, 201)
        return Collection.objects.get(id=res.data['id']), UserCollection.objects.get(id=res.data['user_collection'])

    def items(self, user_collection):
        res = self.client.get(f'/api/user-collections/{user_collection.id}/')
        self.assertEqual(res.status_code, 200)
        return res.data['items']

    def post(self, action, user_collection, **body):
        return self.client.post(f'/api/user-collections/{user_collection.id}/{action}/', {'user_collection_id': str(user_collection.id), **body}, format='json')

    def upload(self, user_collection):
        return self.post('upload', user_collection)

    def as_user(self, user):
        self.client.force_authenticate(user)

    # ---------- tạo -> phát hành -> tải ----------

    def test_create_upload_download_flow(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.assertEqual({i['sense']['id'] for i in self.items(mine)}, {str(self.one.id), str(self.two.id)})  # bản nháp của người tạo
        self.assertFalse(Collection.objects.get(pk=collection.pk).released)

        # Chưa phát hành: người khác không thấy, không tải được
        self.as_user(self.bob)
        self.assertEqual(self.client.post(f'/api/user-collections/{collection.id}/download/').status_code, 404)

        self.as_user(self.alice)
        self.assertEqual(self.upload(mine).status_code, 200)
        collection.refresh_from_db()
        self.assertEqual((collection.version, collection.released, collection.is_uploaded, collection.item_count), (1, True, True, 2))
        self.assertTrue(all(s.is_frozen for s in AISense.objects.filter(id__in=[self.one.id, self.two.id])))

        self.as_user(self.bob)
        self.assertEqual(self.client.post(f'/api/user-collections/{collection.id}/download/').status_code, 201)
        bobs = UserCollection.objects.get(user=self.bob, collection=collection)
        self.assertEqual(bobs.version, 1)  # bắt đầu ở version hiện tại nên thấy ngay các từ đã phát hành
        self.assertEqual({i['sense']['id'] for i in self.items(bobs)}, {str(self.one.id), str(self.two.id)})
        self.assertEqual(Collection.objects.get(pk=collection.pk).downloaded_count, 1)

    def test_upload_without_changes_keeps_version(self):
        collection, mine = self.create_collection(self.one)
        self.assertEqual(self.upload(mine).status_code, 200)
        res = self.upload(mine)  # tác giả: không đổi gì vẫn là cập nhật hợp lệ, không tăng version
        self.assertEqual((res.status_code, res.data['changed']), (200, False))
        collection.refresh_from_db()
        self.assertEqual(collection.version, 1)

    # ---------- sửa nghĩa ----------

    def test_update_released_item_creates_draft_and_keeps_old_version_for_others(self):
        collection, mine = self.create_collection(self.one)
        self.upload(mine)
        self.as_user(self.bob)
        self.client.post(f'/api/user-collections/{collection.id}/download/')
        bobs = UserCollection.objects.get(user=self.bob, collection=collection)

        self.as_user(self.alice)
        released = CollectionItem.objects.get(collection=collection, released=True)
        res = self.client.post('/api/user-collections/update-sense/', {
            'user_collection_id': str(mine.id), 'collection_item_id': str(released.id), 'delta': {'definition': {'en': {'value': 'my def'}}},
        }, format='json')
        self.assertEqual(res.status_code, 200, res.data)

        draft = CollectionItem.objects.get(id=res.data['collection_item_id'])
        self.assertEqual((draft.released, draft.original_id, draft.parent), (False, released.original_id, [released.id]))
        self.assertNotEqual(draft.sense_id, self.one.id)  # nghĩa chính thức không sửa tại chỗ
        self.assertEqual(draft.sense.original_id, self.one.id)
        self.one.refresh_from_db()
        self.assertEqual(self.one.delta, {})

        # Alice thấy bản sửa, Bob (đang ở version 1) vẫn thấy nghĩa cũ
        self.assertEqual([i['sense']['id'] for i in self.items(mine)], [str(draft.sense_id)])
        self.as_user(self.bob)
        self.assertEqual([i['sense']['id'] for i in self.items(bobs)], [str(self.one.id)])

        # Sửa tiếp: dùng lại đúng dòng nháp, sửa tại chỗ vì nghĩa tuỳ chỉnh của chính Alice chưa đóng băng
        self.as_user(self.alice)
        res = self.client.post('/api/user-collections/update-sense/', {
            'user_collection_id': str(mine.id), 'collection_item_id': str(draft.id), 'delta': {'usage': {'en': {'value': 'x'}}},
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['sense_id'], draft.sense_id)
        self.assertEqual(CollectionItem.objects.filter(collection=collection, released=False).count(), 1)

        # Phát hành: bản sửa thay bản cũ ở version 2 và bị đóng băng
        self.assertEqual(self.upload(mine).status_code, 200)
        draft.refresh_from_db()
        self.assertEqual((draft.released, draft.version), (True, 2))
        self.assertTrue(draft.sense.is_frozen)
        self.assertEqual([i['sense']['id'] for i in self.items(mine)], [str(draft.sense_id)])

    def test_update_validation_and_isolation(self):
        collection, mine = self.create_collection(self.one)
        item = CollectionItem.objects.get(collection=collection)
        body = {'user_collection_id': str(mine.id), 'collection_item_id': str(item.id)}
        for bad in ({'user_collection_id': 'x', 'collection_item_id': str(item.id)}, {**body, 'delta': 'str'}, {**body, 'image': 5}):
            self.assertEqual(self.client.post('/api/user-collections/update-sense/', bad, format='json').status_code, 400)

        # Bob không sửa được dòng nháp của Alice, kể cả khi tự có UserCollection trên cùng bộ
        theirs = UserCollection.objects.create(user=self.bob, collection=collection, version=0)
        self.as_user(self.bob)
        res = self.client.post('/api/user-collections/update-sense/', {'user_collection_id': str(theirs.id), 'collection_item_id': str(item.id), 'delta': {'a': 1}}, format='json')
        self.assertEqual(res.status_code, 404)
        # ... và cũng không dùng UserCollection của Alice
        res = self.client.post('/api/user-collections/update-sense/', {'user_collection_id': str(mine.id), 'collection_item_id': str(item.id), 'delta': {'a': 1}}, format='json')
        self.assertEqual(res.status_code, 404)

    # ---------- xoá nghĩa ----------

    def test_delete_released_item_uses_tombstone_and_add_restores(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.upload(mine)
        released = CollectionItem.objects.get(collection=collection, sense=self.one)

        res = self.post('delete-sense', mine, collection_item_id=str(released.id))
        self.assertEqual(res.status_code, 200, res.data)  # trước đây IntegrityError vì unique (collection, sense)
        self.assertEqual([i['sense']['id'] for i in self.items(mine)], [str(self.two.id)])
        self.assertEqual(self.post('delete-sense', mine, collection_item_id=str(released.id)).status_code, 200)  # gọi lại không nhân bia mộ
        self.assertEqual(CollectionItem.objects.filter(collection=collection, sense=self.one, released=False).count(), 1)

        # Thêm lại đúng từ đó: bỏ bia mộ, bản phát hành hiện lại, không thêm dòng mới
        res = self.post('add-sense', mine, sense_id=str(self.one.id))
        self.assertEqual((res.status_code, res.data.get('restored')), (200, True))
        self.assertEqual(CollectionItem.objects.filter(collection=collection, sense=self.one).count(), 1)
        self.assertEqual({i['sense']['id'] for i in self.items(mine)}, {str(self.one.id), str(self.two.id)})

    def test_delete_then_upload_removes_word_from_next_version(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.upload(mine)
        released = CollectionItem.objects.get(collection=collection, sense=self.one)
        self.post('delete-sense', mine, collection_item_id=str(released.id))
        self.assertEqual(self.upload(mine).status_code, 200)

        collection.refresh_from_db()
        self.assertEqual((collection.version, collection.item_count), (2, 1))
        self.assertEqual(collection.metadatas[-1]['changelog']['deleted'], [str(self.one.id)])
        self.assertEqual([i['sense']['id'] for i in self.items(mine)], [str(self.two.id)])

    def test_delete_draft_reverts_or_disappears(self):
        collection, mine = self.create_collection(self.one)
        self.upload(mine)
        released = CollectionItem.objects.get(collection=collection)
        edited = self.client.post('/api/user-collections/update-sense/', {'user_collection_id': str(mine.id), 'collection_item_id': str(released.id), 'delta': {'a': {'x': 1}}}, format='json')

        res = self.post('delete-sense', mine, collection_item_id=str(edited.data['collection_item_id']))
        self.assertEqual((res.status_code, res.data['action']), (200, 'REVERTED_TO_ORIGINAL'))
        self.assertEqual(res.data['collection_item_id'], released.id)
        self.assertEqual([i['sense']['id'] for i in self.items(mine)], [str(self.one.id)])

        added = self.post('add-sense', mine, sense_id=str(self.two.id))
        res = self.post('delete-sense', mine, collection_item_id=str(added.data['item_id']))
        self.assertEqual(res.data['action'], 'NEW_DRAFT_DELETED')
        self.assertEqual([i['sense']['id'] for i in self.items(mine)], [str(self.one.id)])

    # ---------- thêm nghĩa ----------

    def test_add_sense_rules(self):
        collection, mine = self.create_collection(self.one)
        res = self.post('add-sense', mine, sense_id=str(self.two.id))
        self.assertEqual(res.status_code, 200)
        draft = CollectionItem.objects.get(id=res.data['item_id'])
        self.assertEqual((draft.released, draft.created_by, draft.value, draft.original_id), (False, self.alice, 'two', self.two.id))
        self.assertEqual(self.post('add-sense', mine, sense_id=str(self.two.id)).status_code, 400)  # trùng
        self.assertEqual(self.post('add-sense', mine, sense_id='nope').status_code, 400)

        # Nghĩa tuỳ chỉnh của người khác thì không thêm được (trước đây thêm được bất kỳ id nào)
        word = AIWord.objects.get(value='three')
        private = AISense.objects.create(word=word, word_value='three', language_code='en', is_official=False, created_by=self.bob, contents={})
        self.assertEqual(self.post('add-sense', mine, sense_id=str(private.id)).status_code, 404)
        own = AISense.objects.create(word=word, word_value='three', language_code='en', is_official=False, created_by=self.alice, contents={})
        self.assertEqual(self.post('add-sense', mine, sense_id=str(own.id)).status_code, 200)

    def test_clear_custom_drops_all_drafts(self):
        collection, mine = self.create_collection(self.one)
        self.upload(mine)
        self.post('add-sense', mine, sense_id=str(self.two.id))
        res = self.post('clear-custom', mine)
        self.assertEqual((res.status_code, res.data['cleared_count']), (200, 1))
        self.assertEqual([i['sense']['id'] for i in self.items(mine)], [str(self.one.id)])

    # ---------- từ chờ AI ----------

    def test_pending_words_become_visible_drafts_for_the_owner(self):
        collection, mine = self.create_collection(self.one)
        Collection.objects.filter(pk=collection.pk).update(pending_words=['two'])
        res = self.client.get(f'/api/collections/{collection.id}/')  # xem bộ thì từ chờ được thêm vào
        self.assertEqual(res.status_code, 200)
        self.assertEqual({i['sense']['id'] for i in self.items(mine)}, {str(self.one.id), str(self.two.id)})
        self.assertEqual(Collection.objects.get(pk=collection.pk).item_count, 2)
        # Xem lại không thêm trùng
        Collection.objects.filter(pk=collection.pk).update(pending_words=['two'])
        self.client.get(f'/api/collections/{collection.id}/')
        self.assertEqual(CollectionItem.objects.filter(collection=collection, sense=self.two).count(), 1)

    # ---------- version: chỉ tăng khi nghĩa thật sự đổi, người đã tải cập nhật lên bản mới ----------

    def released_version(self, mine):
        self.assertEqual(self.upload(mine).status_code, 200)
        return Collection.objects.get(pk=mine.collection_id)

    def test_drafts_that_change_nothing_do_not_create_a_version(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.released_version(mine)
        # Xoá rồi thêm lại đúng nghĩa đó: nháp không đổi gì so với bản phát hành
        item = CollectionItem.objects.get(collection=collection, released=True, sense=self.one)
        self.post('delete-sense', mine, collection_item_id=str(item.id))
        res = self.post('add-sense', mine, sense_id=str(self.one.id))
        self.assertEqual(res.status_code, 200)
        res = self.upload(mine)  # tác giả: không đổi gì thì không tăng version nhưng vẫn là cập nhật hợp lệ
        self.assertEqual((res.status_code, res.data['changed'], res.data['current_version']), (200, False, 1))
        collection.refresh_from_db()
        self.assertEqual((collection.version, collection.released), (1, True))
        self.assertFalse(CollectionItem.objects.filter(collection=collection, released=False).exists())

    def test_bundle_carries_own_delete_drafts_so_app_can_count_changes(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.released_version(mine)
        item = CollectionItem.objects.get(collection=collection, released=True, sense=self.one)
        self.post('delete-sense', mine, collection_item_id=str(item.id))
        res = self.client.get(f'/api/user-collections/{mine.id}/bundle/', {'user_lang': 'vi'})
        self.assertEqual(res.status_code, 200)
        drafts = [i for i in res.data['items'] if not i['released']]
        self.assertEqual([(str(i['sense_id']), i['is_deleted']) for i in drafts], [(str(self.one.id), True)])

    def test_first_upload_without_senses_is_refused(self):
        collection, mine = self.create_collection(self.one)
        CollectionItem.objects.filter(collection=collection).delete()
        res = self.upload(mine)
        self.assertEqual((res.status_code, res.data.get('errorCode')), (400, 'no_changes'))

    def test_unpublish_hides_collection_and_republish_restores_it_without_new_version(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.released_version(mine)
        self.as_user(self.bob)
        self.assertEqual(self.client.post(f'/api/user-collections/{collection.id}/download/').status_code, 201)

        self.as_user(self.alice)
        res = self.post('unpublish', mine)
        self.assertEqual((res.status_code, res.data['released'], res.data['version']), (200, False, 1))
        self.assertEqual(self.post('unpublish', mine).status_code, 409)  # đã thu hồi rồi
        collection.refresh_from_db()
        self.assertFalse(collection.released)
        self.assertEqual(CollectionItem.objects.filter(collection=collection, released=True).count(), 2)  # nội dung giữ nguyên

        # Người chưa tải không thấy nữa, người đã tải vẫn còn bộ
        self.as_user(User.objects.create_user('carol'))
        self.assertEqual(self.client.post(f'/api/user-collections/{collection.id}/download/').status_code, 404)
        self.as_user(self.bob)
        self.assertEqual(UserCollection.objects.filter(user=self.bob, collection=collection).count(), 1)

        # Người khác không thu hồi được
        bobs = UserCollection.objects.get(user=self.bob, collection=collection)
        self.assertEqual(self.post('unpublish', bobs).status_code, 403)

        # Phát hành lại không cần thay đổi: công khai lại, version giữ nguyên
        self.as_user(self.alice)
        res = self.post('upload', mine, title='Back')
        self.assertEqual((res.status_code, res.data['changed'], res.data['current_version']), (200, False, 1))
        collection.refresh_from_db()
        self.assertEqual((collection.released, collection.version, collection.name, len(collection.metadatas)), (True, 1, 'Back', 1))

    def test_remix_is_logged_on_both_collections(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.released_version(mine)
        self.as_user(self.bob)
        self.client.post(f'/api/user-collections/{collection.id}/download/')
        bobs = UserCollection.objects.get(user=self.bob, collection=collection)
        self.post('add-sense', bobs, sense_id=str(self.three.id))
        res = self.post('upload', bobs)
        self.assertEqual(res.status_code, 200)

        remix = Collection.objects.get(id=res.data['collection_id'])
        self.assertEqual((str(remix.original_id), str(remix.previous_id), [str(p) for p in remix.parents]), (str(collection.id),) * 2 + ([str(collection.id)],))
        self.assertEqual(remix.metadatas[0]['remixed_from']['collection_id'], str(collection.id))
        collection.refresh_from_db()
        self.assertEqual(collection.remix_count, 1)
        self.assertGreater(collection.score, 0)

    def test_upload_counts_only_real_changes_and_applies_info(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.released_version(mine)
        two = CollectionItem.objects.get(collection=collection, released=True, sense=self.two)
        self.post('delete-sense', mine, collection_item_id=str(two.id))
        self.post('add-sense', mine, sense_id=str(self.three.id))
        res = self.post('upload', mine, title='Renamed', desc='New desc')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['changelog'], {'added': [str(self.three.id)], 'updated': [], 'deleted': [str(self.two.id)]})
        collection.refresh_from_db()
        self.assertEqual((collection.version, collection.name, collection.description, collection.item_count), (2, 'Renamed', 'New desc', 2))

        res = self.client.get(f'/api/collections/{collection.id}/versions/')
        self.assertEqual([v['version'] for v in res.data['versions']], [2, 1])

    def test_downloader_updates_to_new_version_and_keeps_own_drafts(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.released_version(mine)
        self.as_user(self.bob)
        self.client.post(f'/api/user-collections/{collection.id}/download/')
        bobs = UserCollection.objects.get(user=self.bob, collection=collection)
        # Bob sửa nghĩa "one" thành bản riêng (nháp)
        one = CollectionItem.objects.get(collection=collection, released=True, sense=self.one)
        res = self.client.post('/api/user-collections/update-sense/', {
            'user_collection_id': str(bobs.id), 'collection_item_id': str(one.id), 'delta': {'definition': {'en': {'value': 'bob def'}}},
        }, format='json')
        bob_sense = res.data['sense_id']

        # Tác giả đổi "one" sang nghĩa khác và thêm "three" ở v2
        self.as_user(self.alice)
        self.client.post('/api/user-collections/update-sense/', {
            'user_collection_id': str(mine.id), 'collection_item_id': str(one.id), 'delta': {'definition': {'en': {'value': 'alice def'}}},
        }, format='json')
        self.post('add-sense', mine, sense_id=str(self.three.id))
        self.released_version(mine)

        self.as_user(self.bob)
        res = self.client.get(f'/api/user-collections/{bobs.id}/updates/')
        self.assertEqual((res.data['current_version'], res.data['latest_version'], res.data['has_update']), (1, 2, True))
        self.assertEqual([c['version'] for c in res.data['changes']], [2])
        self.assertEqual(res.data['conflicts'], [str(self.one.id)])

        res = self.client.post(f'/api/user-collections/{bobs.id}/update-version/')
        self.assertEqual((res.data['updated'], res.data['current_version'], res.data['conflicts']), (True, 2, [str(self.one.id)]))
        items = {i['sense']['word_value']: i['sense']['id'] for i in self.items(bobs)}
        self.assertEqual(set(items), {'one', 'two', 'three'})  # có từ mới của v2
        self.assertEqual(str(items['one']), str(bob_sense))  # nháp của bob vẫn thắng bản tác giả
        self.assertFalse(self.client.post(f'/api/user-collections/{bobs.id}/update-version/').data['updated'])

    def test_released_collection_info_and_delete(self):
        collection, mine = self.create_collection(self.one)
        self.assertEqual(self.client.patch(f'/api/collections/{collection.id}/', {'name': 'Draft name'}, format='json').status_code, 200)
        self.released_version(mine)
        res = self.client.patch(f'/api/collections/{collection.id}/', {'name': 'x'}, format='json')
        self.assertEqual((res.status_code, res.data['errorCode']), (400, 'collection_released'))

        self.as_user(self.bob)
        self.client.post(f'/api/user-collections/{collection.id}/download/')
        bobs = UserCollection.objects.get(user=self.bob, collection=collection)

        # Tác giả xoá bộ đã phát hành: chỉ ẩn khỏi công khai, bob vẫn học được
        self.as_user(self.alice)
        self.assertEqual(self.client.delete(f'/api/collections/{collection.id}/').status_code, 204)
        collection.refresh_from_db()
        self.assertEqual((collection.is_active, collection.is_deleted), (False, False))
        self.assertTrue(UserCollection.objects.get(pk=mine.pk).is_deleted)
        self.as_user(self.bob)
        self.assertEqual(self.client.get(f'/api/collections/{collection.id}/').status_code, 404)
        self.assertEqual(len(self.items(bobs)), 1)

    def test_public_detail_hides_other_users_drafts(self):
        collection, mine = self.create_collection(self.one)
        self.released_version(mine)
        self.post('add-sense', mine, sense_id=str(self.two.id))  # nháp của tác giả
        self.as_user(self.bob)
        res = self.client.get(f'/api/collections/{collection.id}/')
        self.assertEqual([s['id'] for s in res.data['senses']], [str(self.one.id)])
        self.as_user(self.alice)
        res = self.client.get(f'/api/collections/{collection.id}/')
        self.assertEqual({s['id'] for s in res.data['senses']}, {str(self.one.id), str(self.two.id)})

    # ---------- thêm nhiều, thêm từ, sắp xếp, bộ chứa nghĩa ----------

    def test_add_senses_in_batch(self):
        collection, mine = self.create_collection(self.one)
        _, private = make_word_with_sense('private', is_official=False, created_by=self.bob)
        res = self.client.post(f'/api/user-collections/{mine.id}/add-senses/', {
            'sense_ids': [str(self.two.id), str(self.one.id), str(private.id), str(self.three.id)],
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {'added': [str(self.two.id), str(self.three.id)], 'restored': [], 'exists': [str(self.one.id)], 'unknown': [str(private.id)]})
        self.assertEqual([i['sense']['word_value'] for i in self.items(mine)], ['one', 'two', 'three'])

    @override_settings(CACHES=LOCMEM_CACHE)
    def test_add_words_to_own_collection(self):
        collection, mine = self.create_collection(self.one)
        AIWord.objects.create(value='zzbad', language_code='en', status='REJECTED')
        queue, redis = FakeQueue(), FakeRedis()
        self.addCleanup(WordCacheManager().cache_word_clear_specific, 'en', 'zznew')
        with mock.patch('ai.generation.push_job', queue.push_job), mock.patch('ai.bulk.get_queue_client', lambda: redis):
            res = self.client.post(f'/api/user-collections/{mine.id}/add-words/', {'words': ['Two', 'one', 'zzbad', 'zznew'], 'user_lang': 'vi'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.data['added'], res.data['pending_words'], res.data['invalid_words']), (['two'], ['zznew'], ['zzbad']))
        collection.refresh_from_db()
        self.assertEqual((collection.pending_words, collection.invalid_words), (['zznew'], ['zzbad']))
        self.assertEqual([job['value'] for _, job in queue.jobs], ['zznew'])
        self.assertEqual(redis.sets['collection_id:zznew'], {str(collection.id)})

        # Người đã tải (chuẩn bị remix): chỉ thêm được từ đã có nghĩa chính thức, từ lạ không đẩy AI, không đụng bộ gốc
        self.upload(mine)
        self.as_user(self.bob)
        self.client.post(f'/api/user-collections/{collection.id}/download/')
        bobs = UserCollection.objects.get(user=self.bob, collection=collection)
        res = self.client.post(f'/api/user-collections/{bobs.id}/add-words/', {'words': ['three', 'zznew2'], 'user_lang': 'vi'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.data['added'], res.data['unavailable'], res.data['pending_words']), (['three'], ['zznew2'], []))
        collection.refresh_from_db()
        self.assertEqual(collection.pending_words, ['zznew'])  # không đổi

    def test_reorder(self):
        collection, mine = self.create_collection(self.one, self.two)
        self.upload(mine)
        self.post('add-sense', mine, sense_id=str(self.three.id))
        rows = {i['sense']['word_value']: i['id'] for i in self.items(mine)}
        body = {'items': [{'item_id': str(rows['three']), 'display_order': -1}, {'item_id': str(rows['two']), 'display_order': -0.5}]}
        res = self.client.post(f'/api/user-collections/{mine.id}/reorder/', body, format='json')
        self.assertEqual((res.data['updated'], res.data['skipped']), (2, []))
        self.assertEqual([i['sense']['word_value'] for i in self.items(mine)], ['three', 'two', 'one'])
        self.assertEqual(Collection.objects.get(pk=collection.pk).version, 1)  # thứ tự không tạo version

        # Người đã tải không đổi được thứ tự bản phát hành
        self.as_user(self.bob)
        self.client.post(f'/api/user-collections/{collection.id}/download/')
        bobs = UserCollection.objects.get(user=self.bob, collection=collection)
        res = self.client.post(f'/api/user-collections/{bobs.id}/reorder/', {'items': [{'item_id': str(rows['one']), 'display_order': 9}]}, format='json')
        self.assertEqual((res.data['updated'], res.data['skipped']), (0, [str(rows['one'])]))
        self.assertEqual(self.client.post(f'/api/user-collections/{bobs.id}/reorder/', {'items': [{'item_id': 'x'}]}, format='json').status_code, 400)

    def test_containing(self):
        collection, mine = self.create_collection(self.one, self.two)
        _, other = self.create_collection(self.two)
        res = self.client.get('/api/user-collections/containing/', {'sense_id': str(self.two.id)})
        self.assertEqual({str(r['user_collection_id']) for r in res.data}, {str(mine.id), str(other.id)})
        item = CollectionItem.objects.get(collection=collection, sense=self.two)
        self.post('delete-sense', mine, collection_item_id=str(item.id))
        res = self.client.get('/api/user-collections/containing/', {'sense_id': str(self.two.id)})
        self.assertEqual([str(r['user_collection_id']) for r in res.data], [str(other.id)])

    def test_public_list_filters(self):
        Collection.objects.create(name='Animals EN', language_code='en', released=True, like_count=5, created_by=self.bob)
        Collection.objects.create(name='Food EN', language_code='en', released=True, like_count=9, created_by=self.bob)
        Collection.objects.create(name='Animals JA', language_code='ja', released=True, is_official=True, created_by=self.bob)
        names = lambda **q: [r['name'] for r in self.client.get('/api/collections/', q).data['results']]
        self.assertEqual(names(lang='en', ordering='-like_count'), ['Food EN', 'Animals EN'])
        self.assertEqual(sorted(names(q='animals')), ['Animals EN', 'Animals JA'])
        self.assertEqual(names(official='true'), ['Animals JA'])
        self.create_collection(self.one)
        self.assertEqual(names(mine='true'), ['Mine'])


@override_settings(CACHES=LOCMEM_CACHE)
class BundleTests(APITestCase):
    """GET user-collections/{id}/bundle: gói học offline theo cột SQLite của app."""

    def setUp(self):
        django_cache.clear()
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.client.force_authenticate(self.alice)
        self.collection = Collection.objects.create(name='C', language_code='en', is_official=True, version=1)
        self.word, self.done = make_word_with_sense('cat')
        self.word.ipas = [{'value': '/kæt/', 'label': 'US'}]
        self.word.save()
        self.done.contents = {
            'definition': {'en': {'value': 'A small animal.'}, 'vi': {'value': 'Con mèo.'}, 'fr': {'value': 'Un chat.'}},
            'examples': [{'en': {'value': 'The cat sleeps.'}, 'vi': {'value': 'Mèo ngủ.'}, 'fr': {'value': 'Le chat dort.'}}],
            'translations': {'vi': ['mèo'], 'fr': ['chat']},
        }
        self.done.save()
        _, self.todo = make_word_with_sense('dog')
        for sense in (self.done, self.todo):
            CollectionItem.objects.create(collection=self.collection, sense=sense, original=sense, released=True, version=1, value=sense.word_value)
        _, extra = make_word_with_sense('bird')
        CollectionItem.objects.create(collection=self.collection, sense=extra, original=extra, released=False, created_by=self.bob)  # nháp của bob
        self.user_collection = UserCollection.objects.create(user=self.alice, collection=self.collection, version=1)
        self.queue = mock.patch('ai.translation.queue_translation').start()
        self.addCleanup(mock.patch.stopall)

    def bundle(self, **params):
        return self.client.get(f'/api/user-collections/{self.user_collection.id}/bundle/', {'user_lang': 'vi', **params})

    def test_bundle_has_rows_for_offline_learning(self):
        res = self.bundle()
        self.assertEqual(res.status_code, 200)
        data = res.data
        self.assertFalse(data['unchanged'])
        self.assertEqual(sorted(i['value'] for i in data['items']), ['cat', 'dog'])  # nháp của người khác không lọt
        self.assertEqual(data['user_collection']['collection_id'], self.collection.id)
        word = next(w for w in data['words'] if w['value'] == 'cat')
        self.assertEqual(word['ipas'], [{'value': '/kæt/', 'label': 'US'}])

        sense = next(s for s in data['senses'] if s['id'] == self.done.id)
        self.assertEqual(set(sense['contents']['definition']), {'en', 'vi'})  # chỉ ngôn ngữ gốc + của user
        self.assertEqual(set(sense['contents']['examples'][0]), {'en', 'vi'})
        self.assertEqual(sense['contents']['translations'], {'vi': ['mèo']})
        for column in ('original_id', 'delta', 'audio', 'global_difficulty', 'learner_count'):
            self.assertIn(column, sense)

        # Nghĩa thiếu bản dịch: chỉ báo (pending + số đếm), tải bộ không đẩy việc dịch (dịch lười khi học tới)
        self.assertEqual(data['pending'], [str(self.todo.id)])
        self.assertEqual(data['user_collection']['translation_missing'], 1)
        self.assertEqual(self.queue.call_count, 0)

    def test_etag_skips_unchanged_bundle(self):
        etag = self.bundle().data['etag']
        self.assertEqual(dict(self.bundle(etag=etag).data), {'etag': etag, 'unchanged': True})
        self.assertNotEqual(self.client.get(f'/api/user-collections/{self.user_collection.id}/bundle/', {'user_lang': 'fr'}).data['etag'], etag)

        self.todo.contents = {**self.todo.contents, 'definition': {'en': {'value': 'x'}, 'vi': {'value': 'y'}}}
        self.todo.save()  # dịch bù xong
        self.assertFalse(self.bundle(etag=etag).data['unchanged'])

    def test_bundle_validation(self):
        self.assertEqual(self.client.get(f'/api/user-collections/{self.user_collection.id}/bundle/').status_code, 400)
        theirs = UserCollection.objects.create(user=self.bob, collection=self.collection)
        self.assertEqual(self.client.get(f'/api/user-collections/{theirs.id}/bundle/', {'user_lang': 'vi'}).status_code, 404)
        self.assertEqual(self.client.get('/api/user-collections/not-a-uuid/bundle/', {'user_lang': 'vi'}).status_code, 404)

    def test_download_returns_user_collection_id(self):
        other = Collection.objects.create(name='D', language_code='en', is_official=True)
        res = self.client.post(f'/api/user-collections/{other.id}/download/')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['id'], UserCollection.objects.get(user=self.alice, collection=other).id)
        self.assertEqual(self.client.post(f'/api/user-collections/{other.id}/download/').data['id'], res.data['id'])
