from django.test import SimpleTestCase

from ai.seedflow import quality


def sense(pos='noun', level='A1', definition=None, examples=None, translations=None):
    return {'pos': pos, 'level': level, 'contents': {
        'definition': definition or {'en': {'value': 'A round fruit that grows on trees.'}, 'vi': {'value': 'Một loại quả tròn mọc trên cây.'}},
        'examples': examples if examples is not None else [{'en': {'value': 'She ate an apple at noon.'}, 'vi': {'value': 'Cô ấy ăn một quả táo lúc trưa.'}}],
        'translations': translations or {'vi': ['quả táo']},
    }}


def codes(found):
    return {(i['code'], i['lang']) for i in found}


class CheckSenseTests(SimpleTestCase):
    def check(self, s, langs=('en', 'vi'), word='apple'):
        return quality.check_sense(s, word, 'en', langs)

    def test_clean_sense_has_no_issue(self):
        self.assertEqual(self.check(sense()), [])

    def test_example_left_in_english_is_an_error(self):
        s = sense(examples=[{'en': {'value': 'We live here.'}, 'vi': {'value': 'We live here.'}}])
        self.assertIn(('example_untranslated', 'vi'), codes(self.check(s, word='live')))

    def test_definition_left_in_english(self):
        s = sense(definition={'en': {'value': 'A round fruit.'}, 'vi': {'value': 'A round fruit.'}})
        self.assertIn(('def_untranslated', 'vi'), codes(self.check(s)))

    def test_missing_translations_for_expected_language(self):
        found = codes(self.check(sense(), langs=('en', 'vi', 'zh')))
        self.assertIn(('def_translation_missing', 'zh'), found)
        self.assertIn(('translations_empty', 'zh'), found)

    def test_only_expected_languages_are_checked(self):
        self.assertEqual(self.check(sense(), langs=('en', 'vi')), [])

    def test_wrong_script(self):
        s = sense(definition={'en': {'value': 'A round fruit.'}, 'zh': {'value': 'a round fruit in zh'}}, examples=[], translations={'zh': ['apple']})
        found = codes(self.check(s, langs=('en', 'zh')))
        self.assertIn(('wrong_script', 'zh'), found)

    def test_digits_are_not_wrong_script(self):
        s = sense(definition={'en': {'value': 'The number 2.'}, 'zh': {'value': '数字2。'}}, examples=[], translations={'zh': ['2']})
        self.assertNotIn('wrong_script', {c for c, _ in codes(self.check(s, langs=('en', 'zh'), word='two'))})

    def test_number_lost_in_example(self):
        s = sense(examples=[{'en': {'value': 'He bought 3 apples.'}, 'vi': {'value': 'Anh ấy mua vài quả táo.'}}])
        self.assertIn(('example_number_lost', 'vi'), codes(self.check(s)))

    def test_nonstandard_pos_with_suggestion(self):
        found = self.check(sense(pos='det'))
        self.assertEqual([(i['code'], i['severity']) for i in found], [('pos_nonstandard', 'error')])
        self.assertIn('determiner', found[0]['detail'])

    def test_invalid_level(self):
        self.assertIn(('level_invalid', ''), codes(self.check(sense(level='N3'))))

    def test_archaic_definition_is_flagged(self):
        s = sense(definition={'en': {'value': 'An archaic word meaning to hinder.'}, 'vi': {'value': 'Một từ cổ nghĩa là cản trở.'}})
        self.assertIn(('def_archaic', 'en'), codes(self.check(s)))

    def test_translation_equal_to_headword_in_chinese(self):
        s = sense(translations={'zh': ['apple']}, definition={'en': {'value': 'A fruit.'}, 'zh': {'value': '一种水果。'}}, examples=[])
        self.assertIn(('translation_is_headword', 'zh'), codes(self.check(s, langs=('en', 'zh'))))

    def test_repeated_word_in_spanish_definition(self):
        s = sense(definition={'en': {'value': 'Owned something.'}, 'es': {'value': 'Que poseía, tenía o poseía algo.'}}, examples=[], translations={'es': ['tener']})
        self.assertIn(('repeated_word', 'es'), codes(self.check(s, langs=('en', 'es'))))

    def test_vietnamese_repeated_syllable_is_fine(self):
        s = sense(definition={'en': {'value': 'A person.'}, 'vi': {'value': 'Một người, người khác.'}}, examples=[], translations={'vi': ['người']})
        self.assertNotIn('repeated_word', {c for c, _ in codes(self.check(s))})

    def test_duplicate_senses_in_one_word(self):
        a = sense(pos='verb', definition={'en': {'value': 'To move quickly on foot.'}})
        b = sense(pos='verb', definition={'en': {'value': 'To move quickly on foot.'}})
        found = quality.check_word([a, b], 'run', 'en', ())
        self.assertIn(('duplicate_sense', 'en'), {(i['code'], i['lang']) for _, i in found if _ == 1})

    def test_summary_counts(self):
        found = quality.check_word([sense(pos='det')], 'apple', 'en', ('en', 'vi'))
        self.assertEqual(quality.summarize(found), {'error': 1, 'warn': 0, 'by_code': {'pos_nonstandard': 1}})
