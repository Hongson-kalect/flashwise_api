import io
import os
import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APITestCase

from ai.models import AISense, AIWord
from asset.models import MediaFile
from core import storages

User = get_user_model()
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
MP3 = b'ID3\x04\x00\x00\x00\x00\x00\x00' + b'\x00' * 100


def png(width=2000, height=1000):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), 'red').save(buffer, format='PNG')
    return buffer.getvalue()


class MediaTestBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, True)
        settings_patch = override_settings(
            CACHES=LOCMEM_CACHE, MEDIA_ROOT=self.media_root, R2_ENABLED=False, MEDIA_PUBLIC_URL='http://localhost:8000/upload/',
        )
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.client.force_authenticate(self.alice)

    def upload(self, data, name, kind):
        return self.client.post('/api/media/', {'file': SimpleUploadedFile(name, data), 'kind': kind}, format='multipart')


class UploadTests(MediaTestBase):
    def test_image_is_resized_to_webp(self):
        res = self.upload(png(), 'photo.png', 'image')
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.data['key'].startswith(f'media/{self.alice.pk}/') and res.data['key'].endswith('.webp'))
        self.assertEqual(res.data['url'], 'http://localhost:8000/upload/' + res.data['key'])
        self.assertEqual((res.data['width'], res.data['height']), (1280, 640))
        with Image.open(os.path.join(self.media_root, res.data['key'])) as saved:
            self.assertEqual(saved.format, 'WEBP')
        self.assertEqual([m['id'] for m in self.client.get('/api/media/').data], [res.data['id']])

    def test_rejects_bad_files(self):
        self.assertEqual(self.upload(b'not an image at all', 'x.png', 'image').data['errorCode'], 'invalid_file')
        self.assertEqual(self.upload(b'random bytes here!!', 'x.mp3', 'audio').data['errorCode'], 'invalid_file')
        self.assertEqual(self.upload(MP3, 'x.mp3', 'video').data['errorCode'], 'invalid_kind')
        self.assertEqual(self.client.post('/api/media/', {'kind': 'image'}, format='multipart').data['errorCode'], 'file_missing')
        with override_settings(MEDIA_AUDIO_MAX_BYTES=10):
            self.assertEqual(self.upload(MP3, 'x.mp3', 'audio').status_code, 413)
        self.assertFalse(MediaFile.objects.exists())

    def test_audio_quota_and_delete(self):
        res = self.upload(MP3, 'say.mp3', 'audio')
        self.assertEqual((res.status_code, res.data['content_type']), (201, 'audio/mpeg'))
        with override_settings(MEDIA_UPLOADS_PER_DAY=1):
            self.assertEqual(self.upload(MP3, 'again.mp3', 'audio').data['errorCode'], 'quota_exceeded')

        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.delete(f"/api/media/{res.data['id']}/").status_code, 404)  # file của người khác
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.delete(f"/api/media/{res.data['id']}/").status_code, 204)
        self.assertFalse(os.path.exists(os.path.join(self.media_root, res.data['key'])))

    def test_r2_backend(self):
        client = mock.Mock()
        with override_settings(R2_ENABLED=True, R2_BUCKET='bucket', MEDIA_PUBLIC_URL='https://media.example.com/'), \
                mock.patch('core.storages._r2', return_value=client):
            res = self.upload(MP3, 'say.mp3', 'audio')
            self.assertEqual(res.data['url'], 'https://media.example.com/' + res.data['key'])
        kwargs = client.put_object.call_args.kwargs
        self.assertEqual((kwargs['Bucket'], kwargs['Key'], kwargs['ContentType']), ('bucket', res.data['key'], 'audio/mpeg'))
        self.assertFalse(os.listdir(self.media_root))  # không ghi đĩa

    def test_app_config_has_media_base(self):
        self.assertEqual(self.client.get('/api/app-config/').data['media_base_url'], 'http://localhost:8000/upload/')
        self.assertEqual(storages.media_url('https://cdn.pixabay.com/a.jpg'), 'https://cdn.pixabay.com/a.jpg')


class SenseMediaTests(MediaTestBase):
    def setUp(self):
        super().setUp()
        self.word = AIWord.objects.create(value='cat', language_code='en', status='COMPLETED')
        self.sense = AISense.objects.create(word=self.word, word_value='cat', language_code='en', is_official=True,
                                            contents={'definition': {'en': {'value': 'A small animal.'}}})

    def test_update_sense_with_own_audio_only(self):
        mine = self.upload(MP3, 'me.mp3', 'audio').data['key']
        self.client.force_authenticate(self.bob)
        theirs = self.upload(MP3, 'bob.mp3', 'audio').data['key']
        self.client.force_authenticate(self.alice)

        body = {'sense_id': str(self.sense.id), 'user_lang': 'vi', 'delta': {}}
        self.assertEqual(self.client.put('/api/ai-senses/update-sense/', {**body, 'audio': theirs}, format='json').status_code, 400)
        res = self.client.put('/api/ai-senses/update-sense/', {**body, 'audio': mine, 'image': 'https://cdn.example.com/cat.jpg'}, format='json')
        self.assertEqual(res.status_code, 200)
        custom = AISense.objects.get(pk=res.data['id'])
        self.assertEqual((custom.audio, custom.image_preview), (mine, 'https://cdn.example.com/cat.jpg'))
        self.assertIsNone(AISense.objects.get(pk=self.sense.pk).audio)  # nghĩa chính thức không bị sửa

        # Sửa tiếp không gửi audio: giữ; gửi '' thì bỏ
        res = self.client.put('/api/ai-senses/update-sense/', {**body, 'sense_id': str(custom.id)}, format='json')
        self.assertEqual(AISense.objects.get(pk=res.data['id']).audio, mine)
        res = self.client.put('/api/ai-senses/update-sense/', {**body, 'sense_id': str(custom.id), 'audio': ''}, format='json')
        self.assertIsNone(AISense.objects.get(pk=res.data['id']).audio)

    def test_create_sense_keeps_advanced_content(self):
        res = self.client.post('/api/ai-senses/create-sense/', {
            'word_id': str(self.word.id), 'user_lang': 'en', 'metadata': {},
            'contents': {
                'definition': {'en': {'value': 'A pet.'}},
                'pro_tip': {'en': {'value': 'Say "kitty" to children.'}},
                'etymology': {'en': {'value': 'Old English catt.'}},
                'synonyms': ['kitty', ' puss ', 3],
                'antonyms': [],
                'secret': {'en': {'value': 'dropped'}},
            },
        }, format='json')
        self.assertEqual(res.status_code, 201)
        contents = AISense.objects.get(pk=res.data['data']['id']).contents
        self.assertEqual(contents['pro_tip'], {'en': {'value': 'Say "kitty" to children.'}})
        self.assertEqual(contents['synonyms'], ['kitty', 'puss'])
        self.assertNotIn('secret', contents)
        self.assertNotIn('antonyms', contents)

    def test_deleting_account_removes_unused_files(self):
        key = self.upload(MP3, 'me.mp3', 'audio').data['key']
        self.assertEqual(self.client.delete('/api/me/', {'confirm': True}, format='json').data['deleted_media'], 1)
        self.assertFalse(MediaFile.objects.exists())
        self.assertFalse(os.path.exists(os.path.join(self.media_root, key)))
