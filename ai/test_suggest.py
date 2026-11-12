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
        return [row['value'] for row in res.data]

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
        values = [r['value'] for r in res.data]
        self.assertEqual(values[:2], ['home', 'house'])
        self.assertEqual(res.data[0]['gloss'], 'nhà, nhà ở')

    def test_validation(self):
        for params in ({}, {'q': 'x'}, {'q': 'x', 'lang': 'en', 'mode': 'nope'}):
            self.assertEqual(self.client.get('/api/ai-words/suggest/', params).status_code, 400)

    def test_unofficial_sense_not_suggested(self):
        word = make_word('secret', ['bí mật'])
        AISense.objects.filter(word=word).update(is_official=False)
        self.assertNotIn('secret', self.meaning('bí mật'))
