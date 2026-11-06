"""Ảnh minh hoạ AI (utils/image_gen): khoá dùng lại ảnh, hàng đợi xếp chồng, gắn đúng nghĩa, ComfyUI chưa bật."""
import io
import json
import os
import tempfile
from unittest import mock
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from ai.models import AIWord
from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from core.models.ImageLibrary import ImageContext, ImageLibrary
from utils.image_gen import comfy, service
from utils.image_gen.subject import build_prompt, image_key, provider_id, seed_for
from utils.redis.client import get_queue_client

QUEUE = 'redis_image_gen_test'


def fake_png(size=(1216, 832)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (200, 230, 255)).save(buffer, format='PNG')
    return buffer.getvalue()


class ImageKeyTests(SimpleTestCase):
    def test_key_ignores_case_articles_and_punctuation(self):
        for raw in ('Cat sitting', 'a cat sitting.', 'The  CAT, sitting!', '  an cat sitting '):
            self.assertEqual(image_key(raw), 'cat sitting')
        self.assertEqual(image_key('river bank'), 'river bank')
        self.assertNotEqual(image_key('river bank'), image_key('money bank'))

    def test_empty_or_non_text_gives_empty_key(self):
        for raw in ('', '   ', '...', None, 5):
            self.assertEqual(image_key(raw), '')

    def test_seed_is_stable_and_provider_id_depends_on_style_version(self):
        self.assertEqual(seed_for('cat'), seed_for('cat'))
        self.assertNotEqual(seed_for('cat'), seed_for('dog'))
        with override_settings(IMAGE_GEN_STYLE_VERSION='v1'):
            first = provider_id('cat')
        with override_settings(IMAGE_GEN_STYLE_VERSION='v2'):
            self.assertNotEqual(first, provider_id('cat'))

    def test_prompt_keeps_subject_first_and_adds_style(self):
        prompt = build_prompt('child running.')
        self.assertTrue(prompt.startswith('child running. flat vector illustration'))
        self.assertIn('no text', prompt)


class ComfyClientTests(SimpleTestCase):
    def test_workflow_uses_four_steps_and_seed(self):
        workflow = comfy.build_workflow('a cat', 123)
        self.assertEqual(workflow['8']['inputs']['steps'], 4)
        self.assertEqual(workflow['9']['inputs']['noise_seed'], 123)
        self.assertEqual(workflow['4']['inputs']['text'], 'a cat')
        self.assertEqual((workflow['10']['inputs']['width'], workflow['10']['inputs']['height']), comfy.RENDER_SIZE)

    def test_generate_returns_image_bytes(self):
        def respond(method, url, **kwargs):
            response = mock.Mock(status_code=200)
            if url.endswith('/prompt'):
                response.json.return_value = {'prompt_id': 'p1'}
            elif '/history/' in url:
                response.json.return_value = {'p1': {'status': {'status_str': 'success', 'completed': True},
                                                     'outputs': {'13': {'images': [{'filename': 'a.png', 'subfolder': '', 'type': 'temp'}]}}}}
            else:
                response.content = b'PNGDATA'
            return response

        with mock.patch('utils.image_gen.comfy.requests.request', side_effect=respond):
            self.assertEqual(comfy.generate('a cat', 1), b'PNGDATA')

    def test_connection_error_means_unavailable(self):
        import requests
        with mock.patch('utils.image_gen.comfy.requests.request', side_effect=requests.ConnectionError('refused')):
            with self.assertRaises(comfy.ComfyUnavailable):
                comfy.generate('a cat', 1)

    def test_rejected_workflow_is_comfy_error(self):
        with mock.patch('utils.image_gen.comfy.requests.request', return_value=mock.Mock(status_code=400, text='missing model')):
            with self.assertRaises(comfy.ComfyError):
                comfy.generate('a cat', 1)

    def test_webp_is_resized_to_output_size(self):
        data, size = comfy.to_webp(fake_png())
        self.assertEqual(size, comfy.OUTPUT_SIZE)
        self.assertEqual(Image.open(io.BytesIO(data)).format, 'WEBP')


class ImageGenBase(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(IMAGE_GEN_QUEUE=QUEUE, MEDIA_ROOT=self.media.name, IMAGE_GEN_ENABLED=True)
        override.enable()
        self.addCleanup(override.disable)
        self.redis = get_queue_client()
        self.clean_redis()
        self.addCleanup(self.clean_redis)

    def clean_redis(self):
        self.redis.delete(QUEUE)
        for key in self.redis.scan_iter('image_gen:*'):
            if b'zzimg' in key:
                self.redis.delete(key)

    def make_sense(self, value, lang, subject, kind='object'):
        word = AIWord.objects.create(value=value, language_code=lang, status='COMPLETED')
        meta = AISenseMetadata.objects.create(image_metadata={'subject': subject, 'kind': kind} if subject else {})
        return AISense.objects.create(word=word, word_value=value, language_code=lang, metadata=meta, contents={})

    def queued_jobs(self):
        return [json.loads(raw) for raw in self.redis.lrange(QUEUE, 0, -1)]


class EnqueueTests(ImageGenBase):
    def test_same_subject_in_two_languages_shares_one_job(self):
        en = self.make_sense('zzimgcat', 'en', 'a zzimg cat sitting')
        ja = self.make_sense('zzimg猫', 'ja', 'Zzimg cat sitting.')
        stats = service.enqueue_senses(AISense.objects.filter(id__in=[en.id, ja.id]).select_related('metadata'))
        self.assertEqual((stats['queued'], stats['joined']), (1, 1))
        jobs = self.queued_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['key'], jobs[0]['kind']), ('zzimg cat sitting', 'object'))

    def test_senses_without_drawable_subject_are_skipped(self):
        none_kind = self.make_sense('zzimgthe', 'en', 'the', kind='none')
        no_subject = self.make_sense('zzimgof', 'en', '')
        stats = service.enqueue_senses(AISense.objects.filter(id__in=[none_kind.id, no_subject.id]).select_related('metadata'))
        self.assertEqual((stats['queued'], stats['skipped']), (0, 2))
        self.assertEqual(self.queued_jobs(), [])

    def test_grammar_words_and_generic_subjects_are_not_drawn(self):
        article = self.make_sense('zzimgthe', 'en', 'zzimg specific object')  # model nhỏ trả kind=object cho "the"
        AISense.objects.filter(id=article.id).update(pos='article')
        generic = self.make_sense('zzimgstuff', 'en', 'Specific object')
        AISense.objects.filter(id=generic.id).update(pos='noun')
        senses = AISense.objects.filter(id__in=[article.id, generic.id]).select_related('metadata')
        self.assertEqual([service.sense_subject(s) for s in senses], [('', ''), ('', '')])
        self.assertEqual(service.enqueue_senses(senses)['skipped'], 2)

    def test_sense_that_already_has_image_is_skipped(self):
        sense = self.make_sense('zzimgdog', 'en', 'dog')
        AISense.objects.filter(id=sense.id).update(image_preview='images/v1/x.webp')
        sense.refresh_from_db()
        self.assertEqual(service.enqueue_senses([sense])['skipped'], 1)
        self.assertEqual(self.queued_jobs(), [])

    def test_job_runs_once_and_images_every_waiting_sense(self):
        en = self.make_sense('zzimgcat', 'en', 'zzimg cat')
        ja = self.make_sense('zzimg猫', 'ja', 'a zzimg cat')
        service.enqueue_senses(AISense.objects.filter(id__in=[en.id, ja.id]).select_related('metadata'))
        job = self.queued_jobs()[0]

        with mock.patch('utils.image_gen.comfy.generate', return_value=fake_png()) as generate:
            attached = service.run_job(job)
        generate.assert_called_once()

        self.assertEqual({row['sense_id'] for row in attached}, {str(en.id), str(ja.id)})
        en.refresh_from_db()
        ja.refresh_from_db()
        self.assertTrue(en.image_preview.startswith('images/v1/comfyui_') and en.image_preview.endswith('.webp'))
        self.assertEqual((en.image_preview, en.image_context_id), (ja.image_preview, ja.image_context_id))
        self.assertEqual(ImageLibrary.objects.filter(provider='comfyui').count(), 1)
        self.assertEqual(ImageContext.objects.filter(provider='comfyui', description='zzimg cat').count(), 1)
        self.assertTrue(os.path.exists(os.path.join(self.media.name, en.image_preview)))
        self.assertEqual(self.redis.scard('image_gen:wait:zzimg cat'), 0)
        self.assertFalse(self.redis.exists('image_gen:job:zzimg cat'))

        image = ImageLibrary.objects.get(provider='comfyui')
        self.assertEqual(image.metadata['subject'], 'zzimg cat')
        self.assertEqual(image.attribution['license'], 'Apache-2.0')

    def test_later_sense_with_same_subject_reuses_image_without_queue_or_drawing(self):
        first = self.make_sense('zzimgcat', 'en', 'zzimg cat')
        service.enqueue_senses([AISense.objects.select_related('metadata').get(id=first.id)])
        with mock.patch('utils.image_gen.comfy.generate', return_value=fake_png()):
            service.run_job(self.queued_jobs()[0])
        self.redis.delete(QUEUE)

        later = self.make_sense('zzimggato', 'es', 'The zzimg cat!')
        with mock.patch('utils.image_gen.comfy.generate') as generate:
            stats = service.enqueue_senses([AISense.objects.select_related('metadata').get(id=later.id)])
        generate.assert_not_called()
        self.assertEqual((stats['attached'], stats['queued']), (1, 0))
        self.assertEqual(self.queued_jobs(), [])
        first.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(first.image_preview, later.image_preview)

    def test_different_subjects_get_different_images(self):
        a = self.make_sense('zzimgbat', 'en', 'zzimg bat animal')
        b = self.make_sense('zzimgbat2', 'en', 'zzimg baseball bat')
        stats = service.enqueue_senses(AISense.objects.filter(id__in=[a.id, b.id]).select_related('metadata'))
        self.assertEqual(stats['queued'], 2)

    def test_enqueue_word_images_covers_all_senses_of_the_word(self):
        sense = self.make_sense('zzimgword', 'en', 'zzimg kettle')
        stats = service.enqueue_word_images(sense.word_id)
        self.assertEqual(stats['queued'], 1)


class ProcessJobTests(ImageGenBase):
    JOB = {'key': 'zzimgcat', 'subject': 'zzimgcat', 'kind': 'object', 'attempts': 0}

    def test_comfyui_offline_keeps_job_and_pauses_queue(self):
        with mock.patch('utils.image_gen.service.run_job', side_effect=comfy.ComfyUnavailable('off')), \
                mock.patch('utils.image_gen.service.set_cooldown') as cooldown:
            outcome, job = async_to_sync(service.process_job)(dict(self.JOB))
        self.assertEqual(outcome, 'offline')
        self.assertEqual(job['attempts'], 0)  # chưa bật không tính là lần lỗi
        cooldown.assert_called_once_with('comfyui', service.OFFLINE_COOLDOWN)

    def test_drawing_error_retries_then_drops_and_unlocks(self):
        self.redis.set('image_gen:job:zzimgcat', 1)
        with mock.patch('utils.image_gen.service.run_job', side_effect=comfy.ComfyError('bad')), \
                mock.patch('utils.image_gen.service.asyncio.sleep', AsyncMock()):
            outcome, job = async_to_sync(service.process_job)(dict(self.JOB))
            self.assertEqual((outcome, job['attempts']), ('retry', 1))
            outcome, job = async_to_sync(service.process_job)({**self.JOB, 'attempts': service.MAX_ATTEMPTS - 1})
        self.assertEqual(outcome, 'dropped')
        self.assertFalse(self.redis.exists('image_gen:job:zzimgcat'))

    def test_success_notifies_people_watching(self):
        attached = [{'sense_id': 's1', 'word_id': 'w1', 'word_value': 'cat', 'language_code': 'en', 'url': 'images/v1/a.webp'}]
        with mock.patch('utils.image_gen.service.run_job', return_value=attached), \
                mock.patch('utils.image_gen.service.notify', AsyncMock()) as notify:
            outcome, _ = async_to_sync(service.process_job)(dict(self.JOB))
        self.assertEqual(outcome, 'done')
        notify.assert_awaited_once_with(attached)

    def test_notify_sends_partial_image_and_recaches(self):
        attached = [{'sense_id': 's1', 'word_id': 'w1', 'word_value': 'cat', 'language_code': 'en', 'url': 'images/v1/a.webp'},
                    {'sense_id': 's2', 'word_id': 'w1', 'word_value': 'cat', 'language_code': 'en', 'url': 'images/v1/a.webp'}]
        socket = AsyncMock()
        with mock.patch('utils.image_gen.service._recache_word') as recache, mock.patch('utils.utils.socket.socket_message', socket):
            async_to_sync(service.notify)(attached)
        recache.assert_called_once_with('w1')
        self.assertEqual(socket.call_args.args[1], {'type': 'PARTIAL_IMAGE', 'payload': {'s1': 'images/v1/a.webp', 's2': 'images/v1/a.webp'}})
