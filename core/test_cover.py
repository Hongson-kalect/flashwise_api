"""Ảnh bìa bộ từ: POST user-collections/{id}/cover/ (core/views/UserCollection.py)."""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from asset.models import MediaFile
from core.models import Collection, UserCollection

User = get_user_model()


class CoverTests(APITestCase):
    def setUp(self):
        self.author = User.objects.create_user('author')
        self.other = User.objects.create_user('other')
        self.collection = Collection.objects.create(name='C', language_code='en', created_by=self.author)
        self.mine = UserCollection.objects.create(user=self.author, collection=self.collection)
        self.theirs = UserCollection.objects.create(user=self.other, collection=self.collection)
        self.upload = MediaFile.objects.create(kind='image', key='media/1/a.webp', content_type='image/webp', created_by=self.author)
        self.client.force_authenticate(self.author)

    def cover(self, uc=None, image='media/1/a.webp'):
        return self.client.post(f'/api/user-collections/{(uc or self.mine).id}/cover/', {'image': image}, format='json')

    def test_author_sets_and_removes_cover(self):
        res = self.cover()
        self.assertEqual((res.status_code, res.data['image_url']), (200, 'media/1/a.webp'))
        self.collection.refresh_from_db()
        self.assertEqual(self.collection.image_url, 'media/1/a.webp')
        self.assertEqual(self.cover(image='').data['image_url'], None)  # '' gỡ ảnh, về ảnh tự động
        self.collection.refresh_from_db()
        self.assertIsNone(self.collection.image_url)

    def test_external_url_is_accepted_and_changes_bundle_etag(self):
        before = self.client.get(f'/api/user-collections/{self.mine.id}/bundle/', {'user_lang': 'vi'}).data['etag']
        self.assertEqual(self.cover(image='https://example.com/x.jpg').status_code, 200)
        after = self.client.get(f'/api/user-collections/{self.mine.id}/bundle/', {'user_lang': 'vi'}).data
        self.assertNotEqual(before, after['etag'])  # máy đã tải thấy ảnh mới
        self.assertEqual(after['collection']['image_url'], 'https://example.com/x.jpg')

    def test_invalid_images_are_rejected(self):
        for image in ('media/1/not-mine.webp', 'ftp://x/y.png', 5, None):
            self.assertEqual(self.cover(image=image).status_code, 400, image)
        others = MediaFile.objects.create(kind='image', key='media/2/b.webp', content_type='image/webp', created_by=self.other)
        self.assertEqual(self.cover(image=others.key).status_code, 400)  # khoá file của người khác

    def test_only_author_and_not_after_release(self):
        self.client.force_authenticate(self.other)
        self.assertEqual(self.cover(self.theirs).status_code, 403)  # đã tải bộ nhưng không phải tác giả
        self.assertEqual(self.cover(self.mine).status_code, 404)  # bộ của người khác không thấy được
        self.client.force_authenticate(self.author)
        Collection.objects.filter(pk=self.collection.pk).update(released=True)
        res = self.cover()
        self.assertEqual((res.status_code, res.data['errorCode']), (409, 'collection_released'))

    def test_bundle_tells_who_may_edit(self):
        row = self.client.get(f'/api/user-collections/{self.mine.id}/bundle/', {'user_lang': 'vi'}).data['user_collection']
        self.assertEqual((row['is_author'], row['is_released']), (True, False))
        self.client.force_authenticate(self.other)
        row = self.client.get(f'/api/user-collections/{self.theirs.id}/bundle/', {'user_lang': 'vi'}).data['user_collection']
        self.assertEqual((row['is_author'], row['is_released']), (False, False))

    def test_login_required(self):
        self.client.force_authenticate(None)
        self.assertIn(self.cover().status_code, (401, 403))
