from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from utils.ai import speech
from utils.utils.permissions import SpeechThrottle

User = get_user_model()

URL = '/api/speech/recognize/'
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class CleanTranscriptTests(SimpleTestCase):
    def test_strips_quotes_and_spaces(self):
        self.assertEqual(speech.clean_transcript('  "hello"  \n'), 'hello')
        self.assertEqual(speech.clean_transcript('xin   chào'), 'xin chào')

    def test_empty_marker_and_blank_mean_no_speech(self):
        for raw in ('', '   ', None, '<empty>', '<EMPTY>', ' <empty> '):
            self.assertEqual(speech.clean_transcript(raw), '')

    def test_long_output_is_capped(self):
        self.assertEqual(len(speech.clean_transcript('a ' * 500)), 200)


class TranscribeTests(SimpleTestCase):
    def _response(self, text):
        return mock.Mock(text=text, usage_metadata=None)

    def test_rejects_unknown_mime_without_calling_gemini(self):
        with mock.patch.object(speech.genai, 'Client') as client:
            with self.assertRaises(ValueError):
                speech.transcribe(b'x', 'video/mp4', 'en')
            client.assert_not_called()

    def test_prompt_never_contains_the_expected_word_and_asks_not_to_correct(self):
        prompt = speech._prompt('en')
        self.assertIn('no correction of pronunciation', prompt)
        self.assertIn(speech.EMPTY_MARK, prompt)
        self.assertIn('English', prompt)

    @override_settings(GEMINI_SPEECH_MODEL=['m1', 'm2'])
    def test_falls_back_to_next_model_on_error(self):
        calls = []

        def fake_generate(model, contents, config):
            calls.append(model)
            if model == 'm1':
                raise RuntimeError('overloaded')
            return self._response('hour')

        with mock.patch.object(speech.genai, 'Client') as client:
            client.return_value.models.generate_content.side_effect = fake_generate
            self.assertEqual(speech.transcribe(b'audio', 'audio/aac', 'en'), 'hour')
        self.assertEqual(calls, ['m1', 'm2'])

    @override_settings(GEMINI_SPEECH_MODEL=['m1'])
    def test_all_models_failing_raises_unavailable(self):
        with mock.patch.object(speech.genai, 'Client') as client:
            client.return_value.models.generate_content.side_effect = RuntimeError('down')
            with self.assertRaises(speech.SpeechUnavailable):
                speech.transcribe(b'audio', 'audio/aac', 'en')

    @override_settings(GEMINI_SPEECH_MODEL=['gemini-2.5-flash-lite'])
    def test_empty_marker_returns_empty_string(self):
        with mock.patch.object(speech.genai, 'Client') as client:
            client.return_value.models.generate_content.return_value = self._response('<empty>')
            self.assertEqual(speech.transcribe(b'audio', 'audio/aac', 'vi'), '')


@override_settings(CACHES=LOCMEM_CACHE)
class SpeechViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='speaker', password='pw')

    def _post(self, **data):
        return self.client.post(URL, data, format='multipart')

    def _audio(self, content=b'\x00' * 100, content_type='audio/aac'):
        return SimpleUploadedFile('rec.aac', content, content_type=content_type)

    def test_requires_login(self):
        self.assertEqual(self._post(lang='en', file=self._audio()).status_code, 401)

    def test_validates_input(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self._post(file=self._audio()).data['errorCode'], 'invalid_lang')
        self.assertEqual(self._post(lang='en').data['errorCode'], 'file_missing')
        self.assertEqual(self._post(lang='en', file=self._audio(b'')).data['errorCode'], 'invalid_file')
        self.assertEqual(self._post(lang='en', file=self._audio(b'x' * (1024 * 1024 + 1))).status_code, 413)

    def test_unsupported_audio_type_is_400(self):
        self.client.force_authenticate(self.user)
        res = self._post(lang='en', file=self._audio(content_type='application/pdf'))
        self.assertEqual((res.status_code, res.data['errorCode']), (400, 'invalid_file'))

    def test_returns_text(self):
        self.client.force_authenticate(self.user)
        with mock.patch('ai.views.Speech.speech.transcribe', return_value='hour') as fake:
            res = self._post(lang='en', file=self._audio())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {'text': 'hour', 'empty': False})
        args = fake.call_args.args
        self.assertEqual((args[1], args[2]), ('audio/aac', 'en'))

    def test_no_speech_is_empty(self):
        self.client.force_authenticate(self.user)
        with mock.patch('ai.views.Speech.speech.transcribe', return_value=''):
            res = self._post(lang='vi', file=self._audio())
        self.assertEqual(res.data, {'text': '', 'empty': True})

    def test_gemini_down_is_503(self):
        self.client.force_authenticate(self.user)
        with mock.patch('ai.views.Speech.speech.transcribe', side_effect=speech.SpeechUnavailable('x')):
            res = self._post(lang='en', file=self._audio())
        self.assertEqual((res.status_code, res.data['errorCode']), (503, 'speech_unavailable'))

    @mock.patch.object(SpeechThrottle, 'THROTTLE_RATES', {'speech': '2/hour'})
    def test_speech_throttle(self):
        self.client.force_authenticate(self.user)
        with mock.patch('ai.views.Speech.speech.transcribe', return_value='a'):
            codes = [self._post(lang='en', file=self._audio()).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
