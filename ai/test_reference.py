"""Gợi ý từ nguồn ngoài (Wiktionary) trong prompt draft / refine và lệnh seed_words --ref."""
import json
import tempfile
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from ai.management.commands.seed_words import read_references
from ai.models import AIWord
from utils.ai.prompt import REFERENCE_MAX_ENTRIES, render_reference, render_word_prompt
from utils.ai.refine import render_refine_prompt
from utils.redis.word_init import WordCacheManager

REFERENCE = [
    {'pos': 'noun', 'glosses': ['A lash; a pliant instrument used to create a crack.', 'A blow with a whip.', 'third gloss is dropped']},
    {'pos': 'verb', 'glosses': ['To hit with a whip.']},
]


class ReferencePromptTests(SimpleTestCase):
    def test_render_reference_trims_and_skips_bad_entries(self):
        text = render_reference(REFERENCE + ['nope', {'pos': '', 'glosses': ['x']}, {'pos': 'adj', 'glosses': []}])
        self.assertEqual(text.splitlines(), [
            '- noun: A lash; a pliant instrument used to create a crack. | A blow with a whip.',
            '- verb: To hit with a whip.',
        ])

    def test_render_reference_limits_size(self):
        many = [{'pos': 'noun', 'glosses': ['g' * 500]} for _ in range(20)]
        lines = render_reference(many).splitlines()
        self.assertEqual(len(lines), REFERENCE_MAX_ENTRIES)
        self.assertLess(max(len(line) for line in lines), 200)

    def test_empty_reference_leaves_prompts_unchanged(self):
        for empty in (None, [], [{'pos': 'noun', 'glosses': []}]):
            self.assertEqual(render_word_prompt('whip', 'en', empty), render_word_prompt('whip', 'en'))
            self.assertEqual(render_refine_prompt('whip', 'en', [], empty), render_refine_prompt('whip', 'en', []))
        self.assertNotIn('<reference>', render_word_prompt('whip', 'en'))

    def test_draft_prompt_carries_reference_and_rule(self):
        prompt = render_word_prompt('whip', 'en', REFERENCE)
        self.assertIn('<reference>\n- noun:', prompt)
        self.assertIn('never copy or lightly reword', prompt)
        self.assertTrue(prompt.rstrip().endswith('</reference>'))

    def test_refine_prompt_carries_reference_check(self):
        prompt = render_refine_prompt('whip', 'en', [], REFERENCE)
        self.assertIn('<reference>\n- noun:', prompt)
        self.assertIn('8. Reference:', prompt)


class SeedReferenceTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser('root', password='x')
        self.push = mock.patch('ai.generation.push_job').start()
        mock.patch('ai.management.commands.seed_words.queue_length', return_value=0).start()
        mock.patch('ai.management.commands.seed_words.cooldown_remaining', return_value=0).start()
        mock.patch('ai.management.commands.seed_words.time.sleep').start()
        self.addCleanup(mock.patch.stopall)
        cache = WordCacheManager()
        for word in ('zzwhip', 'zzplain'):
            self.addCleanup(cache.cache_word_clear_specific, 'en', word)
            cache.cache_word_clear_specific('en', word)
        AIWord.objects.filter(value__in=('zzwhip', 'zzplain'), language_code='en').delete()
        self.words = self._file('zzwhip\nzzplain\n')
        self.ref = self._file(
            json.dumps({'rank': 1, 'w': 'ZZWhip', 'entries': REFERENCE}) + '\nnot json\n\n'
            + json.dumps({'w': 'zzempty', 'entries': []}) + '\n')

    def _file(self, text):
        f = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8')
        f.write(text)
        f.close()
        return f.name

    def test_read_references_skips_bad_lines_and_normalizes(self):
        self.assertEqual(read_references(self.ref), {'zzwhip': REFERENCE})

    def test_reference_goes_into_the_job_only_for_words_that_have_one(self):
        out = StringIO()
        call_command('seed_words', self.words, '--lang', 'en', '--base-languages', 'en', '--ref', self.ref, stdout=out)
        self.assertIn("'with_ref': 1", out.getvalue())
        jobs = {c.args[1]['value']: c.args[1] for c in self.push.call_args_list}
        self.assertEqual(jobs['zzwhip']['reference'], REFERENCE)
        self.assertNotIn('reference', jobs['zzplain'])
