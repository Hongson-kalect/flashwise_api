from unittest import mock

from django.test import SimpleTestCase, TestCase

from ai.models import AISense, AIWord
from ai.seedflow import verify, verify_db


def contents(vi_translations=('rất', 'lắm'), vi_example='Trời rất nóng.', zh=True):
    c = {
        'definition': {'en': {'value': 'To a high degree.'}, 'vi': {'value': 'Ở mức độ cao.'}},
        'examples': [{'en': {'value': 'It is very hot.'}, 'vi': {'value': vi_example}}],
        'translations': {'vi': list(vi_translations)},
    }
    if zh:
        c['definition']['zh'] = {'value': '很高的程度。'}
        c['examples'][0]['zh'] = {'value': '天气很热。'}
        c['translations']['zh'] = ['很', '非常']
    return c


def senses():
    return [{'pos': 'adverb', 'level': 'A1', 'contents': contents(zh=False)}]


class VerifyPureTests(SimpleTestCase):
    def test_prompt_lists_senses_and_target_languages_only(self):
        text = verify.render_prompt('very', 'en', senses(), ['en', 'vi'])
        self.assertIn("Headword: 'very'", text)
        self.assertIn('[vi] translations: rất | lắm', text)
        self.assertIn('en example_1: It is very hot.', text)
        self.assertNotIn('[en]', text)
        self.assertIn('exactly one row for EVERY (sense, language) pair', text)

    def test_parse_drops_invalid_fixes(self):
        data = {'fixes': [
            {'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | cực kỳ', 'reason': 'x'},
            {'sense': 5, 'field': 'translations', 'lang': 'vi', 'value': 'a', 'reason': 'sense out of range'},
            {'sense': 0, 'field': 'bogus', 'lang': 'vi', 'value': 'a', 'reason': ''},
            {'sense': 0, 'field': 'definition', 'lang': 'en', 'value': 'a', 'reason': 'source language is not fixed'},
            {'sense': 0, 'field': 'definition', 'lang': 'xx', 'value': 'a', 'reason': 'not a target'},
            {'sense': 0, 'field': 'sense_issue', 'lang': 'en', 'value': 'archaic', 'reason': 'ok'},
            {'sense': 0, 'field': 'definition', 'lang': 'vi', 'value': '', 'reason': 'empty'},
        ]}
        kept = verify.parse_fixes(data, senses(), ['en', 'vi'])
        self.assertEqual([(f['field'], f['lang']) for f in kept], [('translations', 'vi'), ('sense_issue', 'en')])

    def test_parse_ok_only_keeps_real_pairs(self):
        data = {'ok': [{'sense': 0, 'langs': ['vi', 'zh', 'fr']}, {'sense': 9, 'langs': ['vi']}]}
        self.assertEqual(verify.parse_ok(data, senses(), ['en', 'vi', 'zh']), {(0, 'vi')})  # zh không có nội dung, sense 9 không tồn tại

    def test_noops_and_cosmetic_fixes_are_dropped(self):
        fixes = [
            {'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | lắm', 'reason': 'fine'},
            {'sense': 0, 'field': 'definition', 'lang': 'vi', 'value': 'Ở mức độ rất cao.', 'reason': 'Slightly more natural wording'},
            {'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | cực kỳ', 'reason': "'lắm' is a postposition, not a translation"},
        ]
        real, dropped = verify.drop_noops(senses(), fixes)
        self.assertEqual((len(real), dropped), (1, 2))
        self.assertEqual(real[0]['value'], 'rất | cực kỳ')

    def test_evaluate_states(self):
        s = [{'pos': 'adverb', 'level': 'A1', 'contents': contents()}]
        data = {'ok': [{'sense': 0, 'langs': ['zh']}], 'fixes': [{'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | cực kỳ', 'reason': 'lắm is a particle'}]}
        out = verify.evaluate(s, ['en', 'vi', 'zh'], data)
        self.assertEqual({l: r['state'] for l, r in out[0]['langs'].items()}, {'vi': 'fixed', 'zh': 'ok'})
        # model im lặng về một ngôn ngữ -> không được tính là đồng ý
        out = verify.evaluate(s, ['en', 'vi', 'zh'], {'ok': [{'sense': 0, 'langs': ['vi']}], 'fixes': []})
        self.assertEqual({l: r['state'] for l, r in out[0]['langs'].items()}, {'vi': 'ok', 'zh': 'flagged'})

    def test_checks_with_scores(self):
        s = [{'pos': 'adverb', 'level': 'A1', 'contents': contents()}]
        data = {'checks': [
            {'sense': 0, 'lang': 'vi', 'back': 'very', 'verdict': 'ok', 'score': 95},
            {'sense': 0, 'lang': 'zh', 'back': 'very', 'verdict': 'ok', 'score': 55},
            {'sense': 3, 'lang': 'vi', 'back': '', 'verdict': 'ok', 'score': 99},
        ], 'fixes': []}
        out = verify.evaluate(s, ['en', 'vi', 'zh'], data)
        self.assertEqual({l: (r['state'], r['score']) for l, r in out[0]['langs'].items()}, {'vi': ('ok', 95), 'zh': ('flagged', 55)})

    def test_fix_score_is_kept_and_low_score_fix_is_not_applied(self):
        s = [{'pos': 'adverb', 'level': 'A1', 'contents': contents()}]
        fix = {'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | cực kỳ', 'reason': 'lắm is a particle', 'score': 40}
        out = verify.evaluate(s, ['en', 'vi'], {'checks': [{'sense': 0, 'lang': 'vi', 'back': '', 'verdict': 'fix', 'score': 40}], 'fixes': [fix]})
        self.assertEqual((out[0]['langs']['vi']['state'], out[0]['langs']['vi']['score']), ('fixed', 40))

    def test_apply_fixes_returns_before_after(self):
        s = senses()
        fixes = [
            {'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | cực kỳ', 'reason': ''},
            {'sense': 0, 'field': 'definition', 'lang': 'vi', 'value': 'Ở mức độ rất cao.', 'reason': ''},
            {'sense': 0, 'field': 'example_1', 'lang': 'vi', 'value': 'Hôm nay trời rất nóng.', 'reason': ''},
            {'sense': 0, 'field': 'sense_issue', 'lang': 'en', 'value': 'archaic', 'reason': ''},
        ]
        count, notes = verify.apply_fixes(s, fixes)
        self.assertEqual(count, 3)
        c = s[0]['contents']
        self.assertEqual(c['translations']['vi'], ['rất', 'cực kỳ'])
        self.assertEqual(c['definition']['vi']['value'], 'Ở mức độ rất cao.')
        self.assertEqual(c['examples'][0]['vi']['value'], 'Hôm nay trời rất nóng.')
        self.assertEqual(notes[0], {'field': 'translations', 'lang': 'vi', 'before': 'rất | lắm', 'after': 'rất | cực kỳ', 'reason': ''})

    def test_recheck_reports_remaining_layer1_errors(self):
        s = senses()
        s[0]['contents']['examples'][0]['vi'] = {'value': 'It is very hot.'}
        left = verify.recheck('very', 'en', s, ['en', 'vi'])
        self.assertIn('example_untranslated', [iss['code'] for _, iss in left])


class VerifyDbTests(TestCase):
    def make(self, value='very', **kw):
        word = AIWord.objects.create(value=value, language_code='en', status='COMPLETED')
        defaults = dict(word=word, word_value=value, language_code='en', pos='adverb', level='A1', contents=contents())
        defaults.update(kw)
        return word, AISense.objects.create(**defaults)

    def run_apply(self, word, sense, data):
        return verify.apply_to_senses(word.value, 'en', [sense], ['en', 'vi', 'zh'], data, model='haiku')

    def test_ok_for_every_language_sets_ai_verify(self):
        word, sense = self.make()
        counts = self.run_apply(word, sense, {'ok': [{'sense': 0, 'langs': ['vi', 'zh']}], 'fixes': []})
        sense.refresh_from_db()
        self.assertEqual((counts['ok'], counts['fixed']), (2, 0))
        self.assertEqual(sense.ai_verify, ['vi', 'zh'])
        self.assertFalse(sense.verify)
        self.assertEqual(sense.verify_state['vi']['state'], 'ok')
        self.assertTrue(sense.verify_state['zh']['ai_verify'])

    def test_fix_is_applied_and_ai_verify_stays_false(self):
        word, sense = self.make()
        data = {'ok': [{'sense': 0, 'langs': ['zh']}], 'fixes': [{'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | cực kỳ', 'reason': 'lắm is a particle'}]}
        counts = self.run_apply(word, sense, data)
        sense.refresh_from_db()
        self.assertEqual((counts['ok'], counts['fixed']), (1, 1))
        self.assertEqual(sense.contents['translations']['vi'], ['rất', 'cực kỳ'])
        self.assertEqual(sense.ai_verify, ['zh'])  # vi được sửa nên chưa tính là hai nhà đồng ý
        rec = sense.verify_state['vi']
        self.assertEqual((rec['state'], rec['ai_verify']), ('fixed', False))
        self.assertEqual(rec['changes'][0]['before'], 'rất | lắm')
        self.assertTrue(sense.verify_state['zh']['ai_verify'])  # ngôn ngữ khác vẫn được ghi nhận riêng

    def test_low_score_fix_is_kept_as_proposal_not_applied(self):
        word, sense = self.make()
        data = {'checks': [{'sense': 0, 'lang': 'zh', 'back': '', 'verdict': 'ok', 'score': 90}],
                'fixes': [{'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | cực kỳ', 'reason': 'lắm is a particle', 'score': 30}]}
        counts = self.run_apply(word, sense, data)
        sense.refresh_from_db()
        self.assertEqual(sense.contents['translations']['vi'], ['rất', 'lắm'])
        self.assertEqual(sense.verify_state['vi']['state'], 'flagged')
        self.assertEqual(sense.verify_state['vi']['proposed'], ['rất | cực kỳ'])
        self.assertEqual(sense.verify_state['zh']['score'], 90)
        self.assertEqual(sense.ai_verify, ['zh'])

    def test_unsure_ok_does_not_set_ai_verify(self):
        word, sense = self.make()
        data = {'checks': [{'sense': 0, 'lang': 'vi', 'back': '', 'verdict': 'ok', 'score': 95},
                           {'sense': 0, 'lang': 'zh', 'back': '', 'verdict': 'ok', 'score': 60}], 'fixes': []}
        self.run_apply(word, sense, data)
        sense.refresh_from_db()
        self.assertEqual(sense.verify_state['zh']['state'], 'flagged')
        self.assertEqual(sense.ai_verify, ['vi'])

    def test_silence_is_not_agreement(self):
        word, sense = self.make()
        self.run_apply(word, sense, {'ok': [{'sense': 0, 'langs': ['vi']}], 'fixes': []})
        sense.refresh_from_db()
        self.assertEqual(sense.verify_state['zh']['state'], 'flagged')
        self.assertEqual(sense.ai_verify, ['vi'])

    def test_human_verified_sense_is_never_touched(self):
        word, sense = self.make(verify=['vi', 'zh'])
        data = {'ok': [], 'fixes': [{'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | cực kỳ', 'reason': 'x'}]}
        counts = self.run_apply(word, sense, data)
        sense.refresh_from_db()
        self.assertEqual(counts['skipped'], 1)
        self.assertEqual(sense.contents['translations']['vi'], ['rất', 'lắm'])
        self.assertEqual(sense.verify_state, {})

    def test_fix_that_breaks_layer1_is_rejected(self):
        word, sense = self.make()
        data = {'ok': [{'sense': 0, 'langs': ['zh']}], 'fixes': [{'sense': 0, 'field': 'example_1', 'lang': 'vi', 'value': 'It is very hot.', 'reason': 'x'}]}
        counts = self.run_apply(word, sense, data)
        sense.refresh_from_db()
        self.assertEqual(counts['flagged'], 1)
        self.assertEqual(sense.contents['examples'][0]['vi']['value'], 'Trời rất nóng.')  # giữ nguyên
        self.assertEqual(sense.verify_state['vi']['state'], 'flagged')

    def test_sense_issue_blocks_ai_verify_and_is_recorded(self):
        word, sense = self.make()
        data = {'ok': [{'sense': 0, 'langs': ['vi', 'zh']}], 'fixes': [{'sense': 0, 'field': 'sense_issue', 'lang': 'en', 'value': 'archaic sense', 'reason': 'x'}]}
        self.run_apply(word, sense, data)
        sense.refresh_from_db()
        self.assertEqual(sense.ai_verify, [])
        self.assertEqual(sense.verify_state['_issues'], ['archaic sense'])

    def test_human_verified_language_is_untouched_but_others_are_checked(self):
        word, sense = self.make(verify=['vi'])
        data = {'checks': [{'sense': 0, 'lang': 'zh', 'back': '', 'verdict': 'ok', 'score': 90}],
                'fixes': [{'sense': 0, 'field': 'translations', 'lang': 'vi', 'value': 'rất | cực kỳ', 'reason': 'lắm is a particle', 'score': 90}]}
        counts = self.run_apply(word, sense, data)
        sense.refresh_from_db()
        self.assertEqual(counts['skipped'], 0)
        self.assertEqual(sense.contents['translations']['vi'], ['rất', 'lắm'])  # người đã kiểm vi: giữ nguyên
        self.assertNotIn('vi', sense.verify_state)
        self.assertEqual(sense.ai_verify, ['zh'])
        self.assertEqual(sense.verify, ['vi'])  # AI không đổi cột của người

    def test_wildcard_human_verify_skips_everything(self):
        word, sense = self.make(verify=['*'])
        counts = self.run_apply(word, sense, {'checks': [], 'fixes': []})
        self.assertEqual(counts['skipped'], 1)

    def test_build_requests_skips_checked_and_human_verified(self):
        word, sense = self.make()
        requests, meta = verify_db.build_requests([word.id], 'en', ['en', 'vi', 'zh'])
        self.assertEqual(len(requests), 1)
        self.run_apply(word, sense, {'ok': [{'sense': 0, 'langs': ['vi', 'zh']}], 'fixes': []})
        self.assertEqual(verify_db.build_requests([word.id], 'en', ['en', 'vi', 'zh'])[0], [])
        self.assertEqual(len(verify_db.build_requests([word.id], 'en', ['en', 'vi', 'zh'], redo=True)[0]), 1)
        AISense.objects.filter(id=sense.id).update(verify=['vi', 'zh'])
        self.assertEqual(verify_db.build_requests([word.id], 'en', ['en', 'vi', 'zh'], redo=True)[0], [])

    def test_failed_request_changes_nothing(self):
        word, sense = self.make()
        requests, meta = verify_db.build_requests([word.id], 'en', ['en', 'vi', 'zh'])
        bad = {requests[0]['custom_id']: {'ok': False, 'text': '', 'stop_reason': '', 'tokens_in': 0, 'tokens_out': 0, 'error': 'boom'}}
        totals = verify_db.apply_results(meta, bad, 'en')
        sense.refresh_from_db()
        self.assertEqual(totals['failed'], 1)
        self.assertEqual(sense.verify_state, {})
        self.assertEqual(sense.ai_verify, [])


class OnlineVerifyTests(TestCase):
    """Luồng online (get-word) có bước verify như luồng batch: utils/ai/online_verify.verify_word_online."""

    def test_tat_thi_khong_goi_ai(self):
        import asyncio
        from unittest import mock
        from django.test import override_settings
        from utils.ai import online_verify
        with override_settings(ONLINE_VERIFY_ENABLED=False), mock.patch('ai.seedflow.llm.run_online') as run:
            self.assertIsNone(asyncio.run(online_verify.verify_word_online('x', 'en', ['en', 'vi'])))
            run.assert_not_called()

    def test_bat_thi_dung_build_requests_va_apply_results(self):
        import asyncio
        from unittest import mock
        from django.test import override_settings
        from utils.ai import online_verify
        meta = {'w1': ('plan', [], ['en', 'vi'])}

        async def fake_run(requests, concurrency=1):
            return {'w1': {'ok': True}}

        with override_settings(ONLINE_VERIFY_ENABLED=True, SEED_VERIFY_ENABLED=True), \
                mock.patch('ai.seedflow.verify_db.build_requests', return_value=([{'custom_id': 'w1'}], meta)) as build, \
                mock.patch('ai.seedflow.llm.run_online', side_effect=fake_run), \
                mock.patch('ai.seedflow.verify_db.apply_results', return_value={'ok': 3, 'fixed': 0, 'flagged': 1, 'skipped': 0, 'issues': 0, 'failed': 0}) as apply:
            counts = asyncio.run(online_verify.verify_word_online('w1', 'en', ['en', 'vi']))
        build.assert_called_once()
        apply.assert_called_once()
        self.assertEqual(counts['flagged'], 1)

    def test_khong_co_gi_de_kiem_thi_bo_qua(self):
        import asyncio
        from unittest import mock
        from django.test import override_settings
        from utils.ai import online_verify
        with override_settings(ONLINE_VERIFY_ENABLED=True, SEED_VERIFY_ENABLED=True), \
                mock.patch('ai.seedflow.verify_db.build_requests', return_value=([], {})), \
                mock.patch('ai.seedflow.llm.run_online') as run:
            self.assertIsNone(asyncio.run(online_verify.verify_word_online('w1', 'en', ['en'])))
            run.assert_not_called()
