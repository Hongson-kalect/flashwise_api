from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache as django_cache
from django.test import override_settings
from rest_framework.test import APITestCase
from uuid6 import uuid7

from ai.models import AISense, AISenseMetadata, AIWord
from ai.tests import LOCMEM_CACHE, make_sense

User = get_user_model()
URL = '/api/ai-senses/add-items/'
GATE = 'ai.views.AISense.items_gate.evaluate_items'


def verdicts(**kwargs):
    """{từ: True/False/(False, lý do)} -> kết quả giống items_gate.evaluate_items."""
    out = {}
    for word, v in kwargs.items():
        ok, reason = (v, '') if isinstance(v, bool) else v
        out[word] = {'ok': ok, 'reason': reason, 'claude': ok, 'gemini': ok}
    out['_seconds'] = {'claude': 0.1, 'gemini': 0.1}
    return out


@override_settings(CACHES=LOCMEM_CACHE, SENSE_PROPOSE_ALLOW_GUEST=True)
class AddItemsApiTests(APITestCase):
    def setUp(self):
        django_cache.clear()
        self.user = User.objects.create_user('alice')
        self.client.force_authenticate(self.user)
        self.word = AIWord.objects.create(value='teller', language_code='en', status='COMPLETED')
        self.meta = AISenseMetadata.objects.create(advanced={'synonyms': ['cashier']}, tags=['bank'])
        data = make_sense(str(uuid7()), pos='noun', definition='A person who handles money at a bank.')
        data.update(word_id=self.word.id, word_value='teller', is_official=True, metadata=self.meta)
        self.sense = AISense.objects.create(**data)

    def post(self, words, kind='synonyms', sense=None):
        return self.client.post(URL, {'sense_id': str(sense or self.sense.id), 'kind': kind, 'words': words}, format='json')

    def stored(self, kind='synonyms'):
        self.meta.refresh_from_db()
        return (self.meta.advanced or {}).get(kind)

    def test_only_words_accepted_by_both_ais_are_added(self):
        with mock.patch(GATE, new=mock.AsyncMock(return_value=verdicts(clerk=True, banana=(False, 'unrelated')))):
            response = self.post(['clerk', 'banana'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['added'], ['clerk'])
        self.assertEqual(response.data['rejected'], [{'word': 'banana', 'reason': 'unrelated'}])
        self.assertEqual(self.stored(), ['cashier', 'clerk'])  # chỉ thêm, giữ nguyên các từ cũ

    def test_gate_receives_current_list_and_only_new_candidates(self):
        gate = mock.AsyncMock(return_value=verdicts(clerk=True))
        with mock.patch(GATE, new=gate):
            self.post(['Cashier', 'clerk', 'CLERK', '  clerk  '])
        args = gate.await_args.args
        self.assertEqual(args[6], ['clerk'])  # trùng danh sách cũ / trùng nhau (khác hoa thường, khoảng trắng) bị lọc trước khi gọi AI
        self.assertEqual(args[7], ['cashier'])  # danh sách hiện có được gửi kèm

    def test_existing_and_invalid_never_reach_the_ais(self):
        gate = mock.AsyncMock(return_value=verdicts())
        with mock.patch(GATE, new=gate):
            response = self.post(['cashier', 'I went to the bank yesterday and it was closed.', '!!!'])
        gate.assert_not_awaited()
        self.assertEqual(response.data['added'], [])
        self.assertEqual(response.data['existing'], ['cashier'])
        self.assertEqual({r['reason'] for r in response.data['rejected']}, {'sentence', 'not_a_word'})
        self.assertEqual(self.stored(), ['cashier'])

    def test_one_ai_failing_adds_nothing(self):
        with mock.patch(GATE, new=mock.AsyncMock(side_effect=TimeoutError())):
            response = self.post(['clerk'])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['errorCode'], 'ai_unavailable')
        self.assertEqual(self.stored(), ['cashier'])

    @override_settings(SENSE_ITEMS_MAX_TOTAL=3)
    def test_list_has_a_total_cap(self):
        gate = mock.AsyncMock(return_value=verdicts(clerk=True, banker=True))
        with mock.patch(GATE, new=gate):
            response = self.post(['clerk', 'banker', 'agent'])
        self.assertEqual(gate.await_args.args[6], ['clerk', 'banker'])  # 1 có sẵn + 2 mới = 3
        self.assertIn({'word': 'agent', 'reason': 'list_full'}, response.data['rejected'])

    @override_settings(SENSE_ITEMS_MAX_WORDS=2)
    def test_too_many_words_per_request(self):
        self.assertEqual(self.post(['a1', 'b2', 'c3']).status_code, 400)

    def test_bad_input(self):
        self.assertEqual(self.post([]).status_code, 400)
        self.assertEqual(self.post(['clerk'], kind='tags').status_code, 400)  # chỉ đồng nghĩa / trái nghĩa / liên quan
        self.assertEqual(self.post(['clerk'], sense=uuid7()).status_code, 404)
        self.assertEqual(self.client.post(URL, {'sense_id': 'nope', 'kind': 'synonyms', 'words': ['x']}, format='json').status_code, 400)

    def test_creates_metadata_when_sense_has_none(self):
        bare = make_sense(str(uuid7()), pos='noun')
        bare.update(word_id=self.word.id, word_value='teller', is_official=True)
        sense = AISense.objects.create(**bare)
        with mock.patch(GATE, new=mock.AsyncMock(return_value=verdicts(clerk=True))):
            self.assertEqual(self.post(['clerk'], kind='relateds', sense=sense.id).data['added'], ['clerk'])
        sense.refresh_from_db()
        self.assertEqual(sense.metadata.advanced['relateds'], ['clerk'])

    def test_login_required_and_guests_blocked_by_default(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.post(['clerk']).status_code, 401)
        self.client.force_authenticate(self.user)
        with override_settings(SENSE_PROPOSE_ALLOW_GUEST=False):
            response = self.post(['clerk'])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['errorCode'], 'account_required')
