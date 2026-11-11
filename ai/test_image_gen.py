"""Ảnh minh hoạ AI (utils/image_gen): khoá dùng lại ảnh, hàng đợi xếp chồng, gắn đúng nghĩa, ComfyUI chưa bật."""
import io
import json
import os
import tempfile
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from ai.models import AIWord
from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from core.models.ImageLibrary import ImageContext, ImageLibrary
from utils.image_gen import brief, comfy, service
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
        self.assertTrue(prompt.startswith('child running. polished 2D game-asset icon illustration'))
        self.assertIn('no text', prompt)


class ComfyClientTests(SimpleTestCase):
    def test_workflow_uses_four_steps_and_seed(self):
        workflow = comfy.build_workflow('a cat', 123)
        self.assertEqual(workflow['8']['inputs']['steps'], 4)
        self.assertEqual(workflow['9']['inputs']['noise_seed'], 123)
        self.assertEqual(workflow['4']['inputs']['text'], 'a cat')
        self.assertEqual((workflow['10']['inputs']['width'], workflow['10']['inputs']['height']), comfy.render_size())

    def test_render_size_comes_from_settings_with_safe_fallback(self):
        with override_settings(IMAGE_GEN_RENDER_SIZE='1024x704'):
            self.assertEqual(comfy.render_size(), (1024, 704))
        for bad in ('1000x700', 'abc', '4096x4096', '1024'):
            with override_settings(IMAGE_GEN_RENDER_SIZE=bad):
                self.assertEqual(comfy.render_size(), comfy.RENDER_SIZE, bad)
        self.assertEqual(comfy.build_workflow('x', 1, size=(512, 512))['10']['inputs']['width'], 512)

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

    def test_transparent_webp_has_alpha_and_keeps_inner_white(self):
        import numpy as np
        from utils.image_gen.cutout import to_transparent
        arr = np.zeros((60, 60, 3), dtype=np.uint8)
        arr[:] = (255, 0, 255)                      # nền magenta
        arr[10:50, 10:50] = (30, 120, 220)          # thân xanh
        arr[20:40, 20:40] = (255, 255, 255)         # trắng thật bên trong
        arr[28:32, 28:32] = (255, 0, 255)           # lỗ nền bị bao kín
        buffer = io.BytesIO(); Image.fromarray(arr).save(buffer, format='PNG')
        out = np.asarray(to_transparent(buffer.getvalue()))
        self.assertEqual(out[2, 2, 3], 0)           # nền ngoài
        self.assertEqual(out[30, 30, 3], 0)         # lỗ kín cũng bị xoá
        self.assertEqual(out[15, 15, 3], 255)       # thân xanh đặc
        self.assertEqual(tuple(out[22, 22]), (255, 255, 255, 255))  # trắng bên trong giữ nguyên
        data, size = comfy.to_webp(buffer.getvalue(), size=(60, 60), transparent=True)
        self.assertEqual(Image.open(io.BytesIO(data)).mode, 'RGBA')

    def test_lora_node_is_inserted_between_unet_and_guider(self):
        flow = comfy.build_workflow('a cat', 1, lora='x\\y.safetensors', lora_strength=0.6)
        self.assertEqual(flow['14']['class_type'], 'LoraLoaderModelOnly')
        self.assertEqual(flow['14']['inputs']['strength_model'], 0.6)
        self.assertEqual(flow['6']['inputs']['model'], ['14', 0])
        self.assertEqual(comfy.build_workflow('a cat', 1, lora='')['6']['inputs']['model'], ['1', 0])

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

    def make_sense(self, value, lang, subject, kind='object', prompt=''):
        word = AIWord.objects.create(value=value, language_code=lang, status='COMPLETED')
        data = {'subject': subject, 'kind': kind, 'prompt': prompt} if subject else {}
        meta = AISenseMetadata.objects.create(image_metadata=data)
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

    def test_job_carries_drawing_description(self):
        sense = self.make_sense('zzimgladle', 'en', 'zzimg ladle', prompt='a soup ladle with a deep round bowl')
        service.enqueue_senses(AISense.objects.filter(id=sense.id).select_related('metadata'))
        self.assertEqual(self.queued_jobs()[0]['prompt'], 'a soup ladle with a deep round bowl')

    def test_abstract_kind_is_not_drawn(self):
        sense = self.make_sense('zzimghope', 'en', 'zzimg hope', kind='abstract')
        stats = service.enqueue_senses(AISense.objects.filter(id=sense.id).select_related('metadata'))
        self.assertEqual((stats['queued'], stats['skipped']), (0, 1))

    def test_build_prompt_prefers_description(self):
        self.assertTrue(build_prompt('ladle', 'a long soup ladle.').startswith('a long soup ladle. '))
        self.assertTrue(build_prompt('ladle').startswith('ladle. '))

    def test_senses_without_drawable_subject_are_skipped(self):
        none_kind = self.make_sense('zzimgthe', 'en', 'the', kind='none')
        no_subject = self.make_sense('zzimgof', 'en', '')
        stats = service.enqueue_senses(AISense.objects.filter(id__in=[none_kind.id, no_subject.id]).select_related('metadata'))
        self.assertEqual((stats['queued'], stats['skipped']), (0, 2))
        self.assertEqual(self.queued_jobs(), [])

    def test_grammar_words_are_drawn_but_generic_subjects_are_not(self):
        # Mọi nghĩa đều cần 1 ảnh: từ ngữ pháp vẫn vẽ (brief là cảnh người que), chỉ chủ thể quá chung chung (dữ liệu cũ) bị bỏ
        article = self.make_sense('zzimgthe', 'en', 'zzimg person pointing at one apple', kind='scene')
        AISense.objects.filter(id=article.id).update(pos='article')
        generic = self.make_sense('zzimgstuff', 'en', 'Specific object')
        AISense.objects.filter(id=generic.id).update(pos='noun')
        senses = AISense.objects.filter(id__in=[article.id, generic.id]).select_related('metadata').order_by('word_value')
        self.assertEqual([bool(service.sense_subject(s)[0]) for s in senses], [False, True])  # theo thứ tự word_value: stuff, the
        stats = service.enqueue_senses(senses)
        self.assertEqual((stats['queued'], stats['skipped']), (1, 1))

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


class BriefTests(ImageGenBase):
    """Brief ảnh (utils/image_gen/brief.py); gọi AI được thay bằng hàm giả. Mọi nghĩa đều phải ra brief dùng được (không có none)."""

    GOOD = {'image_kind': 'object', 'image_subject': 'Soup Ladle', 'image_prompt': 'A metal soup ladle with a deep round bowl and a long handle.'}

    def make(self, value, definition, with_brief=False, examples=None):
        word = AIWord.objects.create(value=value, language_code='en', status='COMPLETED')
        meta = AISenseMetadata.objects.create(image_metadata={'subject': 'old', 'kind': 'object', 'source': brief.provider()} if with_brief else {})
        contents = {'definition': {'en': {'value': definition}}}
        if examples:
            contents['examples'] = [{'en': {'value': e}} for e in examples]
        return AISense.objects.create(word=word, word_value=value, language_code='en', metadata=meta, contents=contents)

    @staticmethod
    def fake(payloads):
        """payloads: {từ: dict} hoặc {từ: [dict lần 1, dict lần 2...]}; ghi lại các lần gọi vào .calls."""
        calls = []

        async def ask(text):
            calls.append(text)
            for word, payload in payloads.items():
                if f'Word: {word} ' in text:
                    seq = payload if isinstance(payload, list) else [payload]
                    return json.dumps(seq[min(sum(1 for c in calls if f'Word: {word} ' in c) - 1, len(seq) - 1)])
            raise RuntimeError('no payload for ' + text)
        ask.calls = calls
        return ask

    def run_ensure(self, senses, payloads):
        from utils.image_gen.brief import ensure_briefs
        return async_to_sync(ensure_briefs)(senses, ask=self.fake(payloads))

    def reload(self, sense):
        return AISense.objects.select_related('metadata').get(id=sense.id)

    def test_brief_is_saved_in_image_metadata(self):
        sense = self.make('zzbriefladle', 'A deep spoon for serving soup.')
        written = self.run_ensure([self.reload(sense)], {'zzbriefladle': self.GOOD})
        self.assertEqual(written, 1)
        meta = self.reload(sense).metadata.image_metadata
        self.assertEqual((meta['subject'], meta['kind'], meta['source']), ('soup ladle', 'object', brief.provider()))
        self.assertTrue(meta['prompt'].startswith('A metal soup ladle'))

    def test_usage_example_is_sent_to_the_model(self):
        sense = self.make('zzbriefex', 'To criticise.', examples=['Critics knocked the film.'])
        ask = self.fake({'zzbriefex': self.GOOD})
        async_to_sync(brief.make_brief)('zzbriefex', 'verb', 'To criticise.', example=brief.sense_example(self.reload(sense)), ask=ask)
        self.assertIn('Usage example: Critics knocked the film.', ask.calls[0])

    def test_existing_brief_is_kept_and_failures_do_not_raise(self):
        keep = self.make('zzbriefkeep', 'Already has a brief.', with_brief=True)
        bad = self.make('zzbriefbad', 'Model fails here.')
        written = self.run_ensure([self.reload(keep), self.reload(bad)], {})
        self.assertEqual(written, 0)
        self.assertEqual(self.reload(keep).metadata.image_metadata['subject'], 'old')
        self.assertFalse(self.reload(bad).metadata.image_metadata.get('source'))

    def test_brief_from_other_provider_or_legacy_none_is_rewritten(self):
        sense = self.make('zzbriefswitch', 'A thing.', with_brief=True)
        other = 'claude' if brief.provider() == 'gemini' else 'gemini'
        meta = sense.metadata
        meta.image_metadata = {**meta.image_metadata, 'source': other}
        meta.save()
        self.assertEqual(self.run_ensure([self.reload(sense)], {'zzbriefswitch': self.GOOD}), 1)
        self.assertEqual(self.reload(sense).metadata.image_metadata['source'], brief.provider())
        legacy = self.make('zzbrieflegacy', 'Old none.', with_brief=True)
        meta = legacy.metadata
        meta.image_metadata = {**meta.image_metadata, 'kind': 'none'}
        meta.save()
        self.assertEqual(self.run_ensure([self.reload(legacy)], {'zzbrieflegacy': self.GOOD}), 1)
        self.assertEqual(self.reload(legacy).metadata.image_metadata['kind'], 'object')

    def test_unusable_answer_is_retried_once_then_fixed(self):
        text = self.make('zzbrieftext', 'A place.')
        bad_then_good = [{'image_kind': 'object', 'image_subject': 'blood bank', 'image_prompt': "A glass container with a label that says 'blood bank'."}, self.GOOD]
        ask = self.fake({'zzbrieftext': bad_then_good})
        result = async_to_sync(brief.make_brief)('zzbrieftext', 'noun', 'A place.', ask=ask)
        self.assertEqual((len(ask.calls), result['subject']), (2, 'soup ladle'))
        self.assertIn('unusable', ask.calls[1])

    def test_still_unusable_becomes_manual_instead_of_a_blank_fallback_picture(self):
        bad = {'image_kind': 'object', 'image_subject': 'graph envelope', 'image_prompt': 'A black line graph showing a curve.'}
        result = async_to_sync(brief.make_brief)('zzbriefgraph', 'noun', 'A boundary of limits.', ask=self.fake({'zzbriefgraph': bad}))
        self.assertEqual(result, {'kind': 'manual', 'subject': '', 'prompt': ''})

    def test_model_may_answer_manual_without_subject_or_prompt(self):
        ask = self.fake({'zzbriefthe': {'image_kind': 'manual'}})
        result = async_to_sync(brief.make_brief)('zzbriefthe', 'article', 'Used before a noun.', ask=ask)
        self.assertEqual((result['kind'], result['subject'], result['prompt']), ('manual', '', ''))
        self.assertEqual(len(ask.calls), 1)  # manual hợp lệ: không hỏi lại

    def test_no_readable_answer_returns_none_so_it_is_retried_later(self):
        async def ask(text):
            return 'not json'
        self.assertIsNone(async_to_sync(brief.make_brief)('zzbriefnet', 'noun', 'A net.', ask=ask))

    def test_subject_that_does_not_match_the_prompt_is_rejected(self):
        wrong = {'image_kind': 'scene', 'image_subject': 'inkwell', 'image_prompt': 'A fairytale house with tall towers and a wide moat around it.'}
        self.assertFalse(brief.consistent('inkwell', wrong['image_prompt']))
        self.assertTrue(brief.consistent('theatrical masks', 'Two classic theatre masks, one smiling and one frowning.'))
        self.assertTrue(brief.consistent('person feeling sad', 'A round-faced cartoon person with a tear.'))
        result = async_to_sync(brief.make_brief)('zzbriefink', 'adverb', 'In a strict way.', ask=self.fake({'zzbriefink': wrong}))
        self.assertEqual(result['kind'], 'manual')

    def test_none_abstract_or_empty_answers_become_manual_and_generic_subjects_are_replaced(self):
        for answer in ({'image_kind': 'none', 'image_subject': '', 'image_prompt': ''},
                       {'image_kind': 'abstract', 'image_subject': 'idea', 'image_prompt': 'x'}):
            result = async_to_sync(brief.make_brief)('zzbriefhope', 'noun', 'A feeling of expectation.', ask=self.fake({'zzbriefhope': answer}))
            self.assertEqual(result['kind'], 'manual')
        generic = {'image_kind': 'scene', 'image_subject': 'idea', 'image_prompt': 'A stick-figure person standing next to a bright light bulb.'}
        result = async_to_sync(brief.make_brief)('zzbriefhope', 'noun', 'A feeling of expectation.', ask=self.fake({'zzbriefhope': generic}))
        self.assertEqual(result['kind'], 'scene')
        self.assertNotEqual(result['subject'], 'idea')


    # --- danh sách cấm đã nới: chỉ chặn khi brief ĐÒI VẼ chữ / biểu đồ ---

    def test_ban_list_allows_innocent_uses_of_words_and_text(self):
        for subject, prompt in [('person talking', 'A person with an open mouth talking, with a few simple sound waves, using words to explain.'),
                                ('writer', 'A person writing a note by hand at a desk with a pen and paper.'),
                                ('letter envelope', 'A sealed paper envelope with a red wax stamp, seen from the front.'),
                                ('person texting', 'A person looking at a phone and smiling while sending a text message.')]:
            self.assertFalse(brief.undrawable(subject, prompt), prompt)

    def test_ban_list_still_blocks_requests_to_draw_text_or_charts(self):
        for prompt in ["A glass jar with a label that says 'honey'.", 'A sign with the word OPEN in big letters.', 'A line graph rising to the right.',
                       'A speech bubble with a red X above a person.', "A wooden board reading 'Welcome' in white letters.",
                       'A sheet of musical notation with notes.', 'A swatch of pale yellow paint.']:
            self.assertTrue(brief.undrawable('thing', prompt), prompt)

    # --- vẽ trước refine ---

    def test_prune_removes_dropped_senses_from_wait_sets(self):
        redis = self.redis
        redis.sadd('image_gen:wait:zzimg prune cat', 'keep-1', 'gone-1')
        redis.sadd('image_gen:wait:zzimg prune dog', 'gone-2')
        self.addCleanup(lambda: [redis.delete(k) for k in redis.scan_iter('image_gen:*zzimg prune*')])
        drafts = {'keep-1': {'brief': {'subject': 'zzimg prune cat'}}, 'gone-1': {'brief': {'subject': 'zzimg prune cat'}},
                  'gone-2': {'brief': {'subject': 'Zzimg Prune Dog'}}}
        self.assertEqual(service.prune_draft_senses(drafts, ['keep-1']), 2)
        self.assertEqual({m.decode() for m in redis.smembers('image_gen:wait:zzimg prune cat')}, {'keep-1'})
        self.assertEqual(redis.scard('image_gen:wait:zzimg prune dog'), 0)

    def test_job_is_dropped_without_drawing_when_nobody_waits_any_more(self):
        self.addCleanup(lambda: [self.redis.delete(k) for k in self.redis.scan_iter('image_gen:*zzimgdropkey*')])
        self.redis.set('image_gen:job:zzimgdropkey', '1')
        with mock.patch('utils.image_gen.service.comfy.generate') as generate:
            self.assertEqual(service.run_job({'key': 'zzimgdropkey', 'subject': 'zzimgdropkey'}), [])
        generate.assert_not_called()
        self.assertIsNone(self.redis.get('image_gen:job:zzimgdropkey'))  # khoá mở lại cho lần sau

    def test_image_is_not_saved_when_all_waiters_vanish_while_drawing(self):
        self.addCleanup(lambda: [self.redis.delete(k) for k in self.redis.scan_iter('image_gen:*zzimgvanish*')])
        self.redis.sadd('image_gen:wait:zzimgvanish', 'ghost')

        def draw(prompt, seed, **kwargs):
            self.redis.delete('image_gen:wait:zzimgvanish')  # refine loại nghĩa trong lúc đang vẽ
            return fake_png()

        before = ImageLibrary.objects.count()
        with mock.patch('utils.image_gen.service.comfy.generate', side_effect=draw):
            self.assertEqual(service.run_job({'key': 'zzimgvanish', 'subject': 'zzimgvanish'}), [])
        self.assertEqual(ImageLibrary.objects.count(), before)

    def test_non_strict_reuse_ignores_definition_rewording_but_not_pos(self):
        drafts = {'a': {'sig': ('Rough text.', 'noun'), 'brief': {'subject': 'x'}}}
        polished = [{'id': 'a', 'pos': 'noun', 'contents': {'definition': {'en': 'Polished text.'}}}]
        self.assertEqual(brief.reusable_briefs(drafts, polished, 'en', strict=False), {'a': {'subject': 'x'}})
        self.assertEqual(brief.reusable_briefs(drafts, polished, 'en', strict=True), {})
        repos = [{'id': 'a', 'pos': 'verb', 'contents': {'definition': {'en': 'Rough text.'}}}]
        self.assertEqual(brief.reusable_briefs(drafts, repos, 'en', strict=False), {})


class BatchedBriefTests(SimpleTestCase):
    """Gộp nhiều nghĩa của một từ vào một lệnh (make_briefs)."""
    OK = {'image_kind': 'object', 'image_subject': 'Soup Ladle', 'image_prompt': 'A metal soup ladle with a deep round bowl and a long handle.'}
    ITEMS = [{'key': k, 'pos': 'noun', 'definition': f'meaning {k}', 'example': ''} for k in ('a', 'b', 'c')]

    def test_all_senses_in_one_call(self):
        calls = []

        async def ask_batch(text):
            calls.append(text)
            return json.dumps({'briefs': [{'n': i, **self.OK} for i in (1, 2, 3)]})

        out = async_to_sync(brief.make_briefs)('zzbatch', self.ITEMS, ask_batch=ask_batch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(out), {'a', 'b', 'c'})
        self.assertIn('1. (noun) meaning a', calls[0])
        self.assertIn('3. (noun) meaning c', calls[0])

    def test_missing_or_bad_item_is_asked_again_alone(self):
        singles = []

        async def ask_batch(text):
            return json.dumps({'briefs': [{'n': 1, **self.OK}, {'n': 2, 'image_kind': 'object', 'image_subject': 'x', 'image_prompt': 'a chart with labels'}]})

        async def ask(text):
            singles.append(text)
            return json.dumps(self.OK)

        out = async_to_sync(brief.make_briefs)('zzbatch', self.ITEMS, ask_batch=ask_batch, ask=ask)
        self.assertEqual(set(out), {'a', 'b', 'c'})
        self.assertEqual(len(singles), 2)  # b (vẽ biểu đồ) và c (thiếu trong kết quả)
        self.assertTrue(all('meaning b' in t or 'meaning c' in t for t in singles))

    def test_failed_batch_falls_back_to_single_calls(self):
        async def ask_batch(text):
            raise RuntimeError('boom')

        async def ask(text):
            return json.dumps(self.OK)

        out = async_to_sync(brief.make_briefs)('zzbatch', self.ITEMS, ask_batch=ask_batch, ask=ask)
        self.assertEqual(set(out), {'a', 'b', 'c'})

    def test_more_than_batch_size_is_split(self):
        calls = []
        items = [{'key': str(i), 'pos': 'noun', 'definition': f'm{i}', 'example': ''} for i in range(10)]

        async def ask_batch(text):
            calls.append(text)
            count = len(text.splitlines()) - 2
            return json.dumps({'briefs': [{'n': i, **self.OK} for i in range(1, count + 1)]})

        out = async_to_sync(brief.make_briefs)('zzbatch', items, ask_batch=ask_batch)
        self.assertEqual(len(calls), 2)  # 8 + 2
        self.assertEqual(len(out), 10)


class BackfillTests(ImageGenBase):
    """get-word trả từ đã xong mà còn nghĩa chưa có ảnh: xếp việc vẽ bù (service.request_missing_images)."""

    def lock_cleanup(self, sense):
        key = f'image_gen:backfill:{sense.word_id}'
        self.redis.delete(key)
        self.addCleanup(self.redis.delete, key)

    def test_word_missing_images_queues_one_prepare_job(self):
        sense = self.make_sense('zzimgbackfill', 'en', 'zzimg cat')
        self.lock_cleanup(sense)
        self.assertTrue(service.request_missing_images(sense.word_id))
        jobs = self.queued_jobs()
        self.assertEqual([(j['type'], j['word_id']) for j in jobs], [('prepare', str(sense.word_id))])
        self.assertFalse(service.request_missing_images(sense.word_id))  # trong thời gian chờ: không xếp thêm
        self.assertEqual(len(self.queued_jobs()), 1)

    def test_payload_with_all_images_skips_database(self):
        sense = self.make_sense('zzimgbfpay', 'en', 'zzimg owl')
        self.lock_cleanup(sense)
        payload = {'senses': {'a': {'image_preview': 'images/v1/a.webp'}, 'b': {'image_preview': 'images/v1/b.webp'}}}
        with self.assertNumQueries(0):
            self.assertFalse(service.request_missing_images(sense.word_id, payload))
        self.assertEqual(self.queued_jobs(), [])
        self.assertTrue(service.request_missing_images(sense.word_id, {'senses': {'a': {'image_preview': None}}}))

    def test_every_sense_without_image_is_queued_even_legacy_none_or_grammar(self):
        done = self.make_sense('zzimgbfdone', 'en', 'zzimg dog')
        AISense.objects.filter(id=done.id).update(image_preview='images/v1/x.webp')
        self.lock_cleanup(done)
        self.assertFalse(service.request_missing_images(done.word_id))  # đã có ảnh: không xếp
        legacy = self.make_sense('zzimgbfnone', 'en', 'zzimg idea', kind='none')
        self.lock_cleanup(legacy)
        self.assertTrue(service.request_missing_images(legacy.word_id))
        grammar = self.make_sense('zzimgbfthe', 'en', 'zzimg the')
        AISense.objects.filter(id=grammar.id).update(pos='article')
        self.lock_cleanup(grammar)
        self.assertTrue(service.request_missing_images(grammar.word_id))
        self.assertEqual(len(self.queued_jobs()), 2)

    @override_settings(IMAGE_GEN_ENABLED=False)
    def test_disabled_never_queues(self):
        sense = self.make_sense('zzimgbfoff', 'en', 'zzimg cow')
        self.lock_cleanup(sense)
        self.assertFalse(service.request_missing_images(sense.word_id))
        self.assertEqual(self.queued_jobs(), [])

    def test_prepare_job_runs_brief_then_enqueue(self):
        with mock.patch('utils.image_gen.service.prepare_and_enqueue', new_callable=AsyncMock) as prepare:
            outcome, _ = async_to_sync(service.process_job)({'type': 'prepare', 'word_id': 'w1', 'attempts': 0})
        self.assertEqual(outcome, 'done')
        prepare.assert_awaited_once_with('w1')


class ParallelBriefTests(BriefTests):
    """Brief chạy song song với refine: nghĩa mất / sửa bị loại, nghĩa còn lại đẩy vào hàng đợi vẽ ngay."""

    def sense_obj(self, sid, definition, pos='noun', example='An example.'):
        return {'id': sid, 'pos': pos, 'contents': {'definition': {'en': definition}, 'examples': [{'en': example}]}}

    def test_draft_briefs_keyed_by_sense_id_with_signature(self):
        senses = [self.sense_obj('s1', 'A deep spoon for soup.'), self.sense_obj('s2', 'A thing to hit with.')]
        ask = self.fake({'zzdraft': self.GOOD})
        drafts = async_to_sync(brief.draft_briefs)('zzdraft', 'en', senses, ask=ask)
        self.assertEqual(set(drafts), {'s1', 's2'})
        self.assertEqual(drafts['s1']['sig'], ('A deep spoon for soup.', 'noun'))
        self.assertIn('Usage example: An example.', ask.calls[0])

    def test_removed_changed_and_added_senses_lose_their_draft_brief(self):
        drafts = {'keep': {'sig': ('Same text.', 'noun'), 'brief': {'subject': 'a'}},
                  'changed': {'sig': ('Old text.', 'noun'), 'brief': {'subject': 'b'}},
                  'gone': {'sig': ('Removed.', 'noun'), 'brief': {'subject': 'c'}}}
        final = [self.sense_obj('keep', 'Same text.'), self.sense_obj('changed', 'New text.'), self.sense_obj('added', 'Brand new.')]
        self.assertEqual(brief.reusable_briefs(drafts, final, 'en'), {'keep': {'subject': 'a'}})
        self.assertEqual(brief.reusable_briefs(drafts, [self.sense_obj('keep', 'Same text.', pos='verb')], 'en'), {})  # đổi loại từ cũng loại

    def test_a_failing_draft_brief_does_not_break_the_others(self):
        senses = [self.sense_obj('ok', 'Fine.'), self.sense_obj('bad', '')]
        drafts = async_to_sync(brief.draft_briefs)('zzdraftbad', 'en', senses, ask=self.fake({'zzdraftbad': self.GOOD}))
        self.assertEqual(set(drafts), {'ok'})  # nghĩa không có định nghĩa thì bỏ qua

    def test_early_enqueue_queues_one_job_per_new_key_and_skips_ready_keys(self):
        redis = self.redis
        preset = {'11111111-1111-1111-1111-1111111111aa': {'kind': 'object', 'subject': 'zzimg early cat', 'prompt': 'A cat sitting.'},
                  '11111111-1111-1111-1111-1111111111bb': {'kind': 'object', 'subject': 'Zzimg Early Cat', 'prompt': 'Another.'}}
        self.addCleanup(lambda: [redis.delete(k) for k in redis.scan_iter('image_gen:*zzimg early cat*')])
        self.assertEqual(service.early_enqueue(preset), 1)  # cùng khoá: 1 job, hai nghĩa vào set chờ
        jobs = self.queued_jobs()
        self.assertEqual((jobs[0]['key'], jobs[0]['kind'], jobs[0]['prompt']), ('zzimg early cat', 'object', 'A cat sitting.'))
        waiting = {m.decode() for m in redis.smembers('image_gen:wait:zzimg early cat')}
        self.assertEqual(len(waiting), 2)
        self.assertEqual(service.early_enqueue(preset), 0)  # job đang chạy: không đẩy thêm

    def test_prepare_applies_preset_brief_to_saved_senses_without_asking_ai(self):
        sense = self.make('zzpreset', 'A thing.')
        preset = {str(sense.id): {'kind': 'object', 'subject': 'zzimg preset thing', 'prompt': 'A plain preset thing.'}}
        self.lock_cleanup_for(sense)
        with mock.patch('utils.image_gen.brief._ask', side_effect=AssertionError('không được gọi AI')):
            async_to_sync(service.prepare_and_enqueue)(sense.word_id, preset)
        meta = self.reload(sense).metadata.image_metadata
        self.assertEqual((meta['subject'], meta['source']), ('zzimg preset thing', brief.provider()))
        self.assertEqual([j['key'] for j in self.queued_jobs()], ['zzimg preset thing'])

    def lock_cleanup_for(self, sense):
        redis = self.redis
        self.addCleanup(lambda: [redis.delete(k) for k in redis.scan_iter('image_gen:*zzimg preset thing*')])

    def test_run_job_waits_for_senses_to_be_saved_before_attaching(self):
        word = AIWord.objects.create(value='zzimgwait', language_code='en', status='COMPLETED')
        meta = AISenseMetadata.objects.create(image_metadata={})
        sense_id = '22222222-2222-2222-2222-2222222222aa'
        self.redis.sadd('image_gen:wait:zzimgwaitkey', sense_id)
        self.addCleanup(lambda: [self.redis.delete(k) for k in self.redis.scan_iter('image_gen:*zzimgwaitkey*')])
        created = []

        def late_save(seconds):  # nghĩa được lưu sau khi ảnh đã vẽ xong
            created.append(AISense.objects.create(id=sense_id, word=word, word_value='zzimgwait', language_code='en', metadata=meta, contents={}))

        context = ImageContext.objects.create(description='zzimgwaitkey', provider='comfyui')
        with mock.patch('utils.image_gen.service.find_ready', return_value=(context, 'images/v1/w.webp')), \
                mock.patch('utils.image_gen.service.time.sleep', side_effect=late_save):
            attached = service.run_job({'key': 'zzimgwaitkey', 'subject': 'zzimgwaitkey'})
        self.assertEqual([a['sense_id'] for a in attached], [sense_id])


class UsageCounterTests(SimpleTestCase):
    """utils/ai/usage.py: đếm lệnh gọi và token theo loại, không bao giờ làm hỏng lệnh gọi AI."""

    def setUp(self):
        from utils.ai import usage
        self.usage = usage
        patcher = mock.patch.object(usage, 'HASH', 'ai_usage_test')  # không đụng bộ đếm thật (trước đây test xoá số đo của run đang chạy)
        patcher.start()
        self.addCleanup(patcher.stop)
        usage.reset()
        self.addCleanup(usage.reset)

    def test_claude_and_gemini_responses_are_summed_per_kind(self):
        claude_response = SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=200))
        gemini_response = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=500, candidates_token_count=80, thoughts_token_count=20))
        self.usage.record_claude('refine', claude_response)
        self.usage.record_claude('refine', claude_response)
        self.usage.record_gemini('brief', gemini_response)
        snap = self.usage.snapshot()
        self.assertEqual(snap['refine'], {'provider': 'claude', 'calls': 2, 'input': 2000, 'output': 400})
        self.assertEqual(snap['brief'], {'provider': 'gemini', 'calls': 1, 'input': 500, 'output': 100})  # token suy nghĩ tính vào output

    def test_missing_usage_or_redis_error_is_swallowed(self):
        self.usage.record_claude('draft', SimpleNamespace())  # phản hồi không có usage
        self.usage.record_gemini('draft', SimpleNamespace(usage_metadata=None))
        self.assertEqual(self.usage.snapshot(), {})
        with mock.patch('utils.ai.usage.get_queue_client', side_effect=ConnectionError('redis down')):
            self.usage.record_claude('draft', SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1)))  # không ném lỗi


# ParallelBriefTests kế thừa BriefTests để dùng lại hàm hỗ trợ: tắt các test thừa kế để không chạy lặp
for _name in [n for n in dir(BriefTests) if n.startswith('test_') and n not in ParallelBriefTests.__dict__]:
    setattr(ParallelBriefTests, _name, None)
