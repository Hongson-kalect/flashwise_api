from rest_framework.test import APITestCase
from uuid6 import uuid7

from ai.models import AIWord
from ai.models.AISense import AISense
from django.contrib.auth import get_user_model

User = get_user_model()


def make_word(value, translations, definition_vi='', score=0):
    word = AIWord.objects.create(value=value, language_code='en', status='COMPLETED')
    contents = {
        'definition': {'en': {'value': 'A thing.'}, 'vi': {'value': definition_vi or ', '.join(translations)}},
        'translations': {'vi': translations},
    }
    AISense.objects.create(word=word, word_value=value, language_code='en', pos='noun', contents=contents, is_official=True, score=score)
    return word


class SuggestTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('suggester')
        self.client.force_authenticate(self.user)
        make_word('door', ['cửa', 'cổng'])
        make_word('home', ['nhà', 'nhà ở'])
        make_word('easy', ['dễ', 'nhẹ nhàng'])
        make_word('pot', ['nồi', 'chậu'])
        make_word('rice cooker', ['nồi cơm điện'])
        make_word('house', ['ngôi nhà', 'căn nhà'])

    def meaning(self, q):
        res = self.client.get('/api/ai-words/suggest/', {'q': q, 'lang': 'en', 'user_lang': 'vi', 'mode': 'meaning'})
        self.assertEqual(res.status_code, 200)
        return [row['value'] for row in res.data['results']]

    def test_meaning_exact_and_prefix_rank_first(self):
        # "nhà" khớp trọn vẹn (home) trước, rồi khớp đầu từ (house: "ngôi nhà" khớp ở đầu từ thứ hai)
        found = self.meaning('nhà')
        self.assertEqual(found[0], 'home')
        self.assertIn('house', found)
        self.assertNotIn('door', found)

    def test_meaning_prefix_of_word_while_typing(self):
        self.assertEqual(self.meaning('nồ')[:2], ['pot', 'rice cooker'])  # gõ dở vẫn ra, khớp trọn xếp trước khi đã gõ đủ
        self.assertEqual(self.meaning('nồi')[0], 'pot')

    def test_meaning_ignores_accents(self):
        # gõ không dấu vẫn ra từ có dấu: "noi" -> pot (nồi), "dai" không khớp "dễ"
        self.assertEqual(self.meaning('noi')[0], 'pot')
        self.assertEqual(self.meaning('NHA')[0], 'home')

    def test_meaning_does_not_match_mid_word(self):
        self.assertNotIn('easy', self.meaning('nh') and [] or self.meaning('hà'))  # "hà" nằm giữa "nhà" / "nhàng": không khớp

    def test_word_mode_returns_gloss_and_prefix(self):
        res = self.client.get('/api/ai-words/suggest/', {'q': 'ho', 'lang': 'en', 'user_lang': 'vi', 'mode': 'word'})
        self.assertEqual(res.status_code, 200)
        values = [r['value'] for r in res.data['results']]
        self.assertEqual(values[:2], ['home', 'house'])
        self.assertEqual(res.data['results'][0]['gloss'], 'nhà, nhà ở')
        self.assertIsNone(res.data['ai_reason'])

    def test_validation(self):
        for params in ({}, {'q': 'x'}, {'q': 'x', 'lang': 'en', 'mode': 'nope'}):
            self.assertEqual(self.client.get('/api/ai-words/suggest/', params).status_code, 400)

    def test_unofficial_sense_not_suggested(self):
        word = make_word('secret', ['bí mật'])
        AISense.objects.filter(word=word).update(is_official=False)
        self.assertNotIn('secret', self.meaning('bí mật'))


class MeaningLookupTests(APITestCase):
    """Tìm từ theo nghĩa bằng AI (thay bằng bản giả): kiểm duyệt, cache, hạn mức, đối chiếu DB."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = User.objects.create_user('lookuper')
        self.client.force_authenticate(self.user)
        self.pot = make_word('pot', ['nồi', 'chậu'])
        self.calls = []

    def fake(self, data):
        from unittest import mock

        def ask(phrase, language_code, user_language_code):
            self.calls.append(phrase)
            return data

        patcher = mock.patch('ai.meaning_lookup.ask_ai', ask)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, q, ai='1'):
        return self.client.get('/api/ai-words/suggest/', {'q': q, 'lang': 'en', 'user_lang': 'vi', 'mode': 'meaning', 'ai': ai})

    def test_ai_candidates_merge_with_db_and_resolve_sense(self):
        self.fake({'valid': True, 'reason_code': 'ok', 'candidates': [
            {'word': 'saucepan', 'pos': 'noun', 'gloss': 'nồi nhỏ có cán'},
            {'word': 'pot', 'pos': 'noun', 'gloss': 'nồi'},
        ]})
        res = self.get('nồi nấu')
        self.assertEqual(res.data['ai_reason'], 'ok')
        rows = {r['value']: r for r in res.data['results']}
        self.assertEqual(rows['saucepan']['source'], 'ai')
        self.assertFalse(rows['saucepan']['in_db'])  # chưa có trong DB: tạo khi thêm vào bộ
        self.assertTrue(rows['pot']['in_db'])  # đã có: kèm sense_id nghĩa khớp
        self.assertIsNotNone(rows['pot']['sense_id'])

    def test_rejected_phrase_returns_reason_without_rows(self):
        self.fake({'valid': False, 'reason_code': 'offensive', 'candidates': []})
        res = self.get('chửi bậy gì đó dài dòng')
        self.assertEqual((res.data['ai_reason'], res.data['results']), ('offensive', []))

    def test_sentence_filtered_before_ai(self):
        self.fake({'valid': True, 'reason_code': 'ok', 'candidates': []})
        res = self.get('hôm nay tôi muốn đi chợ mua một cái nồi mới ở gần nhà')
        self.assertEqual(res.data['ai_reason'], 'sentence')
        self.assertEqual(self.calls, [])

    def test_result_cached_and_quota_counted_once(self):
        self.fake({'valid': True, 'reason_code': 'ok', 'candidates': [{'word': 'kettle', 'pos': 'noun', 'gloss': 'ấm đun nước'}]})
        self.get('ấm đun')
        self.get('ấm đun')
        self.get('ấm  ĐUN')  # chuẩn hoá khoảng trắng và hoa thường: vẫn trúng cache
        self.assertEqual(len(self.calls), 1)

    def test_quota_exhausted_keeps_db_rows(self):
        from django.test import override_settings

        self.fake({'valid': True, 'reason_code': 'ok', 'candidates': [{'word': 'kettle', 'pos': 'noun', 'gloss': 'ấm'}]})
        with override_settings(MEANING_LOOKUPS_PER_DAY=0):
            res = self.get('nồi')
        self.assertEqual(res.data['ai_reason'], 'quota')
        self.assertIn('pot', [r['value'] for r in res.data['results']])

    def test_ai_error_does_not_break_suggest(self):
        from unittest import mock

        with mock.patch('ai.meaning_lookup.ask_ai', side_effect=RuntimeError('boom')):
            res = self.get('nồi')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['ai_reason'], 'unavailable')

    def test_ai_not_called_without_flag_or_for_word_mode(self):
        self.fake({'valid': True, 'reason_code': 'ok', 'candidates': []})
        self.get('nồi', ai='0')
        self.client.get('/api/ai-words/suggest/', {'q': 'po', 'lang': 'en', 'user_lang': 'vi', 'mode': 'word', 'ai': '1'})
        self.assertEqual(self.calls, [])

    def test_banned_word_not_suggested(self):
        AIWord.objects.create(value='saucepan', language_code='en', status='BANNED')
        self.fake({'valid': True, 'reason_code': 'ok', 'candidates': [{'word': 'saucepan', 'pos': 'noun', 'gloss': 'nồi'}]})
        self.assertNotIn('saucepan', [r['value'] for r in self.get('nồi nhỏ').data['results']])


class SenseChoiceTests(APITestCase):
    """bulk-create nhận senses {từ: sense_id}: dùng đúng nghĩa đã chọn thay vì nghĩa điểm cao nhất."""

    def setUp(self):
        self.user = User.objects.create_user('chooser')
        self.client.force_authenticate(self.user)
        self.word = AIWord.objects.create(value='window', language_code='en', status='COMPLETED')
        common = {'word': self.word, 'word_value': 'window', 'language_code': 'en', 'pos': 'noun', 'is_official': True}
        self.top = AISense.objects.create(score=10, contents={'definition': {'en': {'value': 'A computer window.'}}}, **common)
        self.wanted = AISense.objects.create(score=1, contents={'definition': {'en': {'value': 'A glass window.'}}}, **common)

    def create(self, senses):
        res = self.client.post('/api/collections/bulk-create/', {'name': 'x', 'lang': 'en', 'user_lang': 'vi', 'words': ['window'], 'senses': senses}, format='json')
        self.assertEqual(res.status_code, 201)
        from core.models import CollectionItem

        return [str(i.sense_id) for i in CollectionItem.objects.filter(collection__name='x')]

    def test_default_picks_best_score(self):
        self.assertEqual(self.create({}), [str(self.top.id)])

    def test_chosen_sense_overrides(self):
        self.assertEqual(self.create({'window': str(self.wanted.id)}), [str(self.wanted.id)])

    def test_sense_of_other_word_ignored(self):
        other = AIWord.objects.create(value='door', language_code='en', status='COMPLETED')
        foreign = AISense.objects.create(word=other, word_value='door', language_code='en', pos='noun', is_official=True)
        self.assertEqual(self.create({'window': str(foreign.id)}), [str(self.top.id)])


class OffensiveFilterTests(APITestCase):
    """Từ thô tục: mặc định ẩn khỏi gợi ý và bộ công khai; bật `show_offensive` mới thấy (kèm cờ). Nội dung nhạy cảm bị từ chối hẳn."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = User.objects.create_user('offender')
        self.client.force_authenticate(self.user)
        self.rude = make_word('damn', ['chết tiệt'])
        AISense.objects.filter(word=self.rude).update(is_offensive=True)
        make_word('dam', ['đập'])

    def suggest(self, q, mode='word'):
        return [r['value'] for r in self.client.get('/api/ai-words/suggest/', {'q': q, 'lang': 'en', 'user_lang': 'vi', 'mode': mode}).data['results']]

    def enable(self):
        from user.models import UserSetting

        UserSetting.objects.create(user=self.user, key='show_offensive', value=True)

    def test_hidden_by_default_shown_when_enabled(self):
        self.assertEqual(self.suggest('dam'), ['dam'])
        self.assertEqual(self.suggest('chết', 'meaning'), [])
        self.enable()
        self.assertEqual(self.suggest('dam'), ['dam', 'damn'])
        rows = self.client.get('/api/ai-words/suggest/', {'q': 'chết', 'lang': 'en', 'user_lang': 'vi', 'mode': 'meaning'}).data['results']
        self.assertEqual([(r['value'], r['offensive']) for r in rows], [('damn', True)])

    def test_vulgar_ai_candidate_filtered_unless_enabled(self):
        from unittest import mock

        data = {'valid': True, 'reason_code': 'ok', 'candidates': [{'word': 'bastard', 'pos': 'noun', 'gloss': 'đồ khốn', 'vulgar': True}]}
        url = {'q': 'đồ khốn', 'lang': 'en', 'user_lang': 'vi', 'mode': 'meaning', 'ai': '1'}
        with mock.patch('ai.meaning_lookup.ask_ai', return_value=data):
            self.assertEqual(self.client.get('/api/ai-words/suggest/', url).data['results'], [])
            self.enable()
            self.assertEqual([r['value'] for r in self.client.get('/api/ai-words/suggest/', url).data['results']], ['bastard'])

    def test_sensitive_phrase_rejected_outright(self):
        from unittest import mock

        data = {'valid': False, 'reason_code': 'sensitive', 'candidates': []}
        with mock.patch('ai.meaning_lookup.ask_ai', return_value=data):
            self.enable()  # bật thô tục cũng không mở được nội dung nhạy cảm
            res = self.client.get('/api/ai-words/suggest/', {'q': 'xyz', 'lang': 'en', 'user_lang': 'vi', 'mode': 'meaning', 'ai': '1'})
        self.assertEqual((res.data['ai_reason'], res.data['results']), ('sensitive', []))

    def test_offensive_collection_blocked_unless_enabled(self):
        from core.models import Collection, CollectionItem

        author = User.objects.create_user('author')
        col = Collection.objects.create(name='Rude', language_code='en', created_by=author, released=True, is_active=True, version=1)
        CollectionItem.objects.create(collection=col, sense=AISense.objects.get(word=self.rude), released=True, version=1, created_by=author)
        listed = lambda: [c['name'] for c in self.client.get('/api/collections/', {'lang': 'en'}).data['results']]
        self.assertNotIn('Rude', listed())
        self.assertEqual(self.client.get(f'/api/collections/{col.id}/').status_code, 403)
        self.assertEqual(self.client.post(f'/api/user-collections/{col.id}/download/').data['errorCode'], 'offensive_collection')
        self.enable()
        self.assertIn('Rude', listed())
        self.assertEqual(self.client.get(f'/api/collections/{col.id}/').status_code, 200)

    def test_senses_list_hides_offensive_unless_enabled(self):
        get = lambda: self.client.get('/api/ai-words/senses/', {'lang': 'en', 'user_lang': 'vi', 'words': 'damn'}).data['results']['damn'].get('senses') or []
        self.assertEqual(get(), [])
        self.enable()
        rows = get()
        self.assertEqual([r['offensive'] for r in rows], [True])
