from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache as django_cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase
from uuid6 import uuid7

from ai import form_examples
from ai.models import AISense, AIWord, SenseFormExample, WordForm
from ai.tests import LOCMEM_CACHE, make_sense

User = get_user_model()
URL = '/api/ai-senses/form-example/'
ASK = 'ai.form_examples._ask'


class ContainsFormTests(SimpleTestCase):
    def test_whole_word_only(self):
        self.assertTrue(form_examples.contains_form('She is buying a car.', 'buying'))
        self.assertTrue(form_examples.contains_form('Buying is easy.', 'buying'))
        self.assertFalse(form_examples.contains_form('She bought a car.', 'buying'))
        self.assertFalse(form_examples.contains_form('Rebuying is rare.', 'buying'))

    def test_no_space_languages_match_substring(self):
        self.assertTrue(form_examples._matches('私は本を買っています。', '買って', 'ja'))


@override_settings(CACHES=LOCMEM_CACHE, SENSE_PROPOSE_ALLOW_GUEST=True, FORM_EXAMPLES_PER_DAY=3)
class FormExampleApiTests(APITestCase):
    def setUp(self):
        django_cache.clear()
        self.user = User.objects.create_user('alice')
        self.client.force_authenticate(self.user)
        self.word = AIWord.objects.create(value='buy', language_code='en', status='COMPLETED')
        for form, tag in (('buying', 'verb.ing'), ('bought', 'verb.past')):
            WordForm.objects.create(word=self.word, language_code='en', form=form, pos='verb', tag=tag)
        data = make_sense(str(uuid7()), pos='verb', definition='To get something by paying money.')
        data.update(word_id=self.word.id, word_value='buy', is_official=True)
        self.sense = AISense.objects.create(**data)

    def post(self, form='buying', lang='vi'):
        return self.client.post(URL, {'sense_id': str(self.sense.id), 'form': form, 'user_lang': lang}, format='json')

    def test_creates_checks_and_stores_one_sentence(self):
        ask = mock.AsyncMock(return_value=('She is buying a new car.', 'Cô ấy đang mua xe mới.'))
        with mock.patch(ASK, new=ask):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'form': 'buying', 'text': 'She is buying a new car.', 'created': True, 'translate': 'Cô ấy đang mua xe mới.'})
        self.assertEqual(SenseFormExample.objects.filter(sense=self.sense).count(), 1)
        self.assertIn('buying', ask.await_args.args[0])

    def test_second_call_is_free_and_shared(self):
        with mock.patch(ASK, new=mock.AsyncMock(return_value=('She is buying a car.', 'Cô ấy đang mua xe.'))):
            self.post()
        other = User.objects.create_user('bob')
        self.client.force_authenticate(other)
        ask = mock.AsyncMock()
        with mock.patch(ASK, new=ask):
            response = self.post()
        ask.assert_not_awaited()
        self.assertEqual((response.status_code, response.data['created']), (200, False))

    def test_new_language_only_translates_the_stored_sentence(self):
        with mock.patch(ASK, new=mock.AsyncMock(return_value=('She is buying a car.', 'Cô ấy đang mua xe.'))):
            self.post(lang='vi')
        ask = mock.AsyncMock(return_value=('She is buying a car.', 'Elle achète une voiture.'))
        with mock.patch(ASK, new=ask):
            response = self.post(lang='fr')
        self.assertEqual(response.data['text'], 'She is buying a car.')
        self.assertEqual(response.data['translate'], 'Elle achète une voiture.')
        self.assertEqual(SenseFormExample.objects.filter(sense=self.sense).count(), 1)

    def test_sentence_without_the_form_is_retried_then_dropped(self):
        ask = mock.AsyncMock(return_value=('She bought a car.', 'x'))
        with mock.patch(ASK, new=ask):
            response = self.post()
        self.assertEqual((response.status_code, response.data['errorCode']), (422, 'no_example'))
        self.assertEqual(ask.await_count, form_examples.MAX_ATTEMPTS)
        self.assertFalse(SenseFormExample.objects.exists())

    def test_second_attempt_can_succeed(self):
        ask = mock.AsyncMock(side_effect=[('She bought a car.', 'x'), ('He is buying bread.', 'Anh ấy đang mua bánh.')])
        with mock.patch(ASK, new=ask):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['text'], 'He is buying bread.')

    def test_form_of_another_word_is_rejected_without_ai(self):
        ask = mock.AsyncMock()
        with mock.patch(ASK, new=ask):
            response = self.post(form='running')
        ask.assert_not_awaited()
        self.assertEqual(response.status_code, 400)

    def test_ai_failure_is_503_and_stores_nothing(self):
        with mock.patch(ASK, new=mock.AsyncMock(side_effect=RuntimeError('down'))):
            response = self.post()
        self.assertEqual((response.status_code, response.data['errorCode']), (503, 'ai_unavailable'))
        self.assertFalse(SenseFormExample.objects.exists())

    def test_daily_limit_counts_only_new_generations(self):
        WordForm.objects.create(word=self.word, language_code='en', form='buys', pos='verb', tag='verb.3sg')
        WordForm.objects.create(word=self.word, language_code='en', form='buyer', pos='verb', tag='x1')
        WordForm.objects.create(word=self.word, language_code='en', form='buyed', pos='verb', tag='x2')
        sentences = {'buying': 'I am buying it.', 'bought': 'I bought it.', 'buys': 'She buys it.', 'buyer': 'A buyer came.'}
        def fake(prompt):
            used = prompt.split('Word form to use: ')[1].split(' ')[0]
            return sentences[used], 'dịch'

        with mock.patch(ASK, new=mock.AsyncMock(side_effect=fake)):
            codes = [self.post(form=f).status_code for f in ('buying', 'bought', 'buys', 'buyer')]
            again = self.post(form='buying').status_code  # đọc lại ví dụ có sẵn không tính lượt
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(again, 200)
