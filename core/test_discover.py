from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from core.models import Collection, UserCollection
from user.models import UserSetting
from utils.models.Tag import Tag

User = get_user_model()
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM_CACHE)
class DiscoverTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.travel = Tag.objects.create(name='Travel', names={'vi': 'Du lịch'}, isSystemTag=True, display_order=1)
        self.food = Tag.objects.create(name='Food', isSystemTag=True, display_order=2)
        self.jlpt = Tag.objects.create(name='JLPT N5', languageCode='ja')
        self.plain = Tag.objects.create(name='misc')

        def make(name, tags=(), score=0, **extra):
            collection = Collection.objects.create(**{'name': name, 'language_code': 'en', 'released': True, 'version': 1, 'created_by': self.bob, 'score': score, **extra})
            collection.tags.set(tags)
            return collection

        self.trip = make('Trip', [self.travel], score=10)
        self.cook = make('Cook', [self.food], score=50)
        self.both = make('Trip food', [self.travel, self.food], score=5)
        self.top = make('Top', [], score=90)
        self.draft = make('Draft', [self.travel], released=False)  # bộ nháp của bob: không công khai

    def test_tags_list(self):
        res = self.client.get('/api/tags/', {'lang': 'en', 'topics': 'true', 'user_lang': 'vi'})
        self.assertEqual([(t['name'], t['collection_count']) for t in res.data], [('Du lịch', 2), ('Food', 2)])
        names = [t['name'] for t in self.client.get('/api/tags/', {'lang': 'en'}).data]
        self.assertIn('misc', names)
        self.assertNotIn('JLPT N5', names)  # tag riêng tiếng Nhật
        self.assertIn('JLPT N5', [t['name'] for t in self.client.get('/api/tags/', {'lang': 'ja'}).data])

    def test_filter_by_tag_and_ordering(self):
        res = self.client.get('/api/collections/', {'lang': 'en', 'tag': str(self.travel.id), 'ordering': 'popular'})
        self.assertEqual([c['name'] for c in res.data['results']], ['Trip', 'Trip food'])
        self.assertEqual(res.data['count'], 2)
        self.assertIn(self.travel.id, res.data['results'][0]['tags'])
        res = self.client.get('/api/collections/', {'lang': 'en', 'tag': f'{self.travel.id},{self.food.id}', 'ordering': 'newest'})
        self.assertEqual([c['name'] for c in res.data['results']], ['Trip food', 'Cook', 'Trip'])

    def test_suggested_uses_interests_then_score(self):
        res = self.client.get('/api/collections/suggested/', {'lang': 'en', 'tags': f'{self.travel.id},{self.food.id}'})
        self.assertEqual([c['name'] for c in res.data['results']], ['Trip food', 'Cook', 'Trip', 'Top'])
        self.assertEqual(res.data['results'][0]['matched_tags'], 2)

        # Đăng nhập: lấy chủ đề từ setting, bỏ bộ đã tải
        self.client.force_authenticate(self.alice)
        UserSetting.objects.create(user=self.alice, key='interest_tags', value=[str(self.food.id)])
        UserCollection.objects.create(user=self.alice, collection=self.cook, version=1)
        res = self.client.get('/api/collections/suggested/', {'lang': 'en', 'limit': 2})
        self.assertEqual([c['name'] for c in res.data['results']], ['Trip food', 'Top'])
        self.assertEqual(self.client.get('/api/collections/suggested/').status_code, 400)

        # Không có chủ đề: theo điểm
        UserSetting.objects.filter(user=self.alice).delete()
        res = self.client.get('/api/collections/suggested/', {'lang': 'en'})
        self.assertEqual([c['name'] for c in res.data['results']], ['Top', 'Trip', 'Trip food'])

    def test_view_count_once_per_viewer_per_day(self):
        self.client.force_authenticate(self.alice)
        for _ in range(3):
            self.client.get(f'/api/collections/{self.trip.id}/')
        self.client.force_authenticate(self.bob)
        self.client.get(f'/api/collections/{self.trip.id}/')  # tác giả xem bộ mình
        self.client.force_authenticate(None)
        self.client.get(f'/api/collections/{self.trip.id}/')
        res = self.client.get(f'/api/collections/{self.trip.id}/')
        self.assertEqual(res.data['view_count'], 2)

    def test_my_uploads(self):
        Collection.objects.filter(pk=self.trip.pk).update(view_count=7, like_count=2, downloaded_count=3)
        Collection.objects.filter(pk=self.cook.pk).update(view_count=1, is_active=False)
        self.assertEqual(self.client.get('/api/me/uploads/').status_code, 401)
        self.client.force_authenticate(self.bob)
        res = self.client.get('/api/me/uploads/')
        self.assertEqual((res.data['collection_count'], res.data['view_count'], res.data['like_count'], res.data['downloaded_count']), (4, 8, 2, 3))
        self.assertFalse(next(c for c in res.data['collections'] if c['name'] == 'Cook')['is_active'])
