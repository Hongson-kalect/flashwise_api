"""Phiên âm Latin cho ja / zh / ko bằng thuật toán (utils/ai/romanize.py): không DB, không AI."""
from django.test import SimpleTestCase

from utils.ai.romanize import add_readings, reading_ipas, romanize


class KoreanTests(SimpleTestCase):
    CASES = {
        '학교': 'hakgyo', '감사합니다': 'gamsahamnida', '안녕하세요': 'annyeonghaseyo', '독립': 'dongnip', '신라': 'silla',
        '같이': 'gachi', '굳이': 'guji', '해돋이': 'haedoji', '닫히다': 'dachida', '좋아요': 'joayo', '좋고': 'joko',
        '값이': 'gapsi', '많이': 'mani', '읽다': 'ikda', '앉다': 'anda', '있어요': 'isseoyo', '맛있다': 'masitda',
        '꽃이': 'kkochi', '먹는': 'meongneun', '입니다': 'imnida', '국화': 'gukwa', '한국어': 'hangugeo',
        '칼날': 'kallal', '심리': 'simni', '밖': 'bak', '부엌': 'bueok', '옷이': 'osi', '닭이': 'dalgi',
    }

    def test_known_words(self):
        for word, expected in self.CASES.items():
            self.assertEqual(romanize(word, 'ko'), expected, word)

    def test_sentence_keeps_spaces_and_punctuation(self):
        self.assertEqual(romanize('나는 매일 아침 학교에 간다.', 'ko'), 'naneun maeil achim hakgyoe ganda.')

    def test_no_script_returns_empty(self):
        self.assertEqual(romanize('hello', 'ko'), '')


class ChineseTests(SimpleTestCase):
    def test_word_joined_with_tone_marks(self):
        self.assertEqual(romanize('银行', 'zh', as_word=True), 'yínháng')
        self.assertEqual(romanize('学习', 'zh', as_word=True), 'xuéxí')
        self.assertEqual(romanize('西安', 'zh', as_word=True), "xī'ān")

    def test_sentence_one_syllable_per_space_and_punctuation(self):
        self.assertEqual(romanize('我下午要去银行取一些钱。', 'zh'), 'wǒ xià wǔ yào qù yín háng qǔ yī xiē qián.')
        self.assertEqual(romanize('他说：“你好！”', 'zh'), 'tā shuō: “nǐ hǎo!”')
        self.assertEqual(romanize('他有3个苹果，我有5个。', 'zh'), 'tā yǒu 3 gè píng guǒ, wǒ yǒu 5 gè.')

    def test_polyphone_uses_phrase(self):
        self.assertEqual(romanize('行走', 'zh', as_word=True), 'xíngzǒu')
        self.assertEqual(romanize('银行', 'zh', as_word=True), 'yínháng')

    def test_long_phrase_stays_spaced(self):
        self.assertEqual(romanize('一石二鸟', 'zh', as_word=True), 'yī shí èr niǎo')


class JapaneseTests(SimpleTestCase):
    def test_particles_long_vowels_and_sokuon(self):
        self.assertEqual(romanize('こんにちは', 'ja'), 'konnichiwa')
        self.assertEqual(romanize('学校', 'ja'), 'gakkō')
        self.assertEqual(romanize('東京', 'ja'), 'tōkyō')
        self.assertEqual(romanize('私は猫を飼っています。', 'ja'), 'watakushi wa neko o katte imasu.')
        self.assertEqual(romanize('切手', 'ja'), 'kitte')
        self.assertEqual(romanize('雑誌', 'ja'), 'zasshi')

    def test_copula_separate_other_auxiliaries_attached(self):
        self.assertEqual(romanize('学生です', 'ja'), 'gakusei desu')
        self.assertEqual(romanize('一緒に行きましょう', 'ja'), 'issho ni ikimashō')

    def test_loanwords_and_quotes(self):
        self.assertEqual(romanize('コーヒー', 'ja'), 'kōhī')
        self.assertEqual(romanize('ファイル', 'ja'), 'fairu')
        self.assertEqual(romanize('「こんにちは」と言った。', 'ja'), '“konnichiwa” to itta.')

    def test_headword_joined(self):
        self.assertEqual(romanize('食べ物', 'ja', as_word=True), 'tabemono')


class ContentsTests(SimpleTestCase):
    def test_add_readings_fills_roman_and_removes_stale(self):
        contents = {
            'definition': {'zh': {'value': '一种金融机构。', 'roman': 'sai'}, 'en': {'value': 'A bank.'}},
            'usage': {'zh': {'value': ''}},
            'examples': [{'zh': {'value': '我去银行。'}, 'vi': {'value': 'Tôi đi ngân hàng.'}}],
        }
        add_readings(contents, 'zh')
        self.assertEqual(contents['definition']['zh']['roman'], 'yī zhǒng jīn róng jī gòu.')
        self.assertNotIn('roman', contents['usage']['zh'])
        self.assertEqual(contents['examples'][0]['zh']['roman'], 'wǒ qù yín háng.')
        self.assertNotIn('roman', contents['definition']['en'])
        self.assertNotIn('roman', contents['examples'][0]['vi'])

    def test_japanese_also_gets_ruby(self):
        contents = {'definition': {'ja': {'value': '猫がいる。'}}, 'examples': []}
        add_readings(contents, 'ja')
        self.assertEqual(contents['definition']['ja']['roman'], 'neko ga iru.')
        self.assertIn('猫', contents['definition']['ja']['ruby'])

    def test_other_languages_untouched(self):
        contents = {'definition': {'en': {'value': 'A bank.'}}, 'examples': []}
        add_readings(contents, 'en')
        self.assertEqual(contents, {'definition': {'en': {'value': 'A bank.'}}, 'examples': []})


class IpasTests(SimpleTestCase):
    def test_zh_keeps_ai_tones_when_syllables_match(self):
        self.assertEqual(reading_ipas('行', 'zh', [{'label': 'READING', 'value': 'xíng'}]),
                         [{'label': 'READING', 'value': 'xíng', 'roman': 'xíng'}])

    def test_zh_replaces_ipa_with_pinyin(self):
        self.assertEqual(reading_ipas('学习', 'zh', [{'label': 'READING', 'value': '/ɕɥɛ˧˥ ɕi˧˥/'}]),
                         [{'label': 'READING', 'value': 'xuéxí', 'roman': 'xuéxí'}])

    def test_ko_value_is_hangul(self):
        self.assertEqual(reading_ipas('학교', 'ko', [{'label': 'READING', 'value': '/hak.k͈jo/'}]),
                         [{'label': 'READING', 'value': '학교', 'roman': 'hakgyo'}])

    def test_ja_uses_ai_kana_or_computed(self):
        self.assertEqual(reading_ipas('猫', 'ja', [{'label': 'READING', 'value': 'ねこ'}]),
                         [{'label': 'READING', 'value': 'ねこ', 'roman': 'neko'}])
        self.assertEqual(reading_ipas('猫', 'ja', [{'label': 'READING', 'value': 'cat'}]),
                         [{'label': 'READING', 'value': 'ねこ', 'roman': 'neko'}])

    def test_latin_languages_pass_through(self):
        ipas = [{'label': 'US', 'value': '/bæŋk/'}]
        self.assertEqual(reading_ipas('bank', 'en', ipas), ipas)
