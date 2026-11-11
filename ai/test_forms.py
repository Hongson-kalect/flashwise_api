from unittest import mock

from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase
from uuid6 import uuid7

from ai.forms import build_forms, forms_of, guess_lemmas, resolve_form
from ai.models import AISense, AIWord, WordForm
from ai.tests import LOCMEM_CACHE, FakeQueue, make_sense
from utils import morph
from utils.redis.word_init import WordCacheManager


def need(test, lang, probe):
    """Bỏ qua test nếu thư viện của ngôn ngữ đó chưa cài trong môi trường chạy."""
    if not probe():
        test.skipTest(f'chưa cài thư viện biến cách cho {lang}')


class MorphTests(TestCase):
    def tags(self, form, lang):
        return {(a.lemma, a.pos, a.tag) for a in morph.analyze(form, lang)}

    def test_english_irregular_forms_resolve_to_lemma(self):
        need(self, 'en', lambda: morph.analyze('ran', 'en'))
        self.assertIn(('run', 'verb', 'verb.past'), self.tags('ran', 'en'))
        self.assertIn(('child', 'noun', 'noun.plural'), self.tags('children', 'en'))
        self.assertIn(('good', 'adj', 'adj.comparative'), self.tags('better', 'en'))
        self.assertIn(('tall', 'adj', 'adj.comparative'), self.tags('taller', 'en'))

    def test_ambiguous_form_gives_every_candidate(self):
        need(self, 'en', lambda: morph.analyze('ran', 'en'))
        self.assertEqual({(lemma, pos) for lemma, pos, _ in self.tags('banks', 'en')}, {('bank', 'noun'), ('bank', 'verb')})
        self.assertEqual({tag for _, _, tag in self.tags('hung', 'en')}, {'verb.past', 'verb.pp'})

    def test_lemma_and_nonsense_are_not_forms(self):
        need(self, 'en', lambda: morph.analyze('ran', 'en'))
        self.assertEqual(morph.analyze('run', 'en'), [])
        self.assertEqual(morph.analyze('xqzvbn', 'en'), [])
        self.assertEqual(morph.analyze('', 'en'), [])

    def test_english_paradigm_marks_irregular(self):
        need(self, 'en', lambda: morph.paradigm('run', 'verb', 'en'))
        by_tag = {e.tag: e for e in morph.paradigm('run', 'verb', 'en')}
        self.assertEqual(by_tag['verb.past'].form, 'ran')
        self.assertTrue(by_tag['verb.past'].irregular)
        self.assertEqual(by_tag['verb.ing'].form, 'running')
        self.assertFalse(by_tag['verb.ing'].irregular)
        self.assertNotIn('verb.pp', by_tag)  # run = run: trùng từ gốc nên không có dòng riêng

    def test_pos_names_from_senses_are_normalized(self):
        self.assertEqual(morph.normalize_pos('Adjective'), 'adj')
        self.assertIsNone(morph.normalize_pos('idiom'))
        need(self, 'en', lambda: morph.paradigm('good', 'adjective', 'en'))
        self.assertEqual({e.form for e in morph.paradigm('good', 'adjective', 'en')}, {'better', 'best'})

    def test_other_languages(self):
        need(self, 'ru', lambda: morph.analyze('бежал', 'ru'))
        self.assertIn('бежать', {a.lemma for a in morph.analyze('бежал', 'ru')})
        need(self, 'ja', lambda: morph.analyze('食べた', 'ja'))
        self.assertIn(('食べる', 'verb', 'verb.past'), self.tags('食べた', 'ja'))
        self.assertEqual(morph.analyze('食べる', 'ja'), [])
        need(self, 'ko', lambda: morph.analyze('먹었다', 'ko'))
        self.assertIn('먹다', {a.lemma for a in morph.analyze('먹었다', 'ko')})
        need(self, 'fr', lambda: morph.analyze('mangeaient', 'fr'))
        self.assertIn('manger', {a.lemma for a in morph.analyze('mangeaient', 'fr')})

    def test_languages_without_inflection_and_unknown_never_raise(self):
        for lang in ('zh', 'vi', 'th', 'xx'):
            self.assertEqual(morph.analyze('abc', lang), [])
            self.assertEqual(morph.paradigm('abc', 'verb', lang), [])


def add_sense(word, pos):
    data = make_sense(str(uuid7()), pos=pos)
    data.update(word_id=word.id, word_value=word.value, is_official=True)
    return AISense.objects.create(**data)


class FormsServiceTests(TestCase):
    def setUp(self):
        need(self, 'en', lambda: morph.analyze('ran', 'en'))

    def lemma(self, value, pos='verb', language='en', status='COMPLETED'):
        word = AIWord.objects.create(value=value, language_code=language, status=status)
        add_sense(word, pos)
        return word

    def test_build_forms_is_idempotent_and_per_pos(self):
        run = self.lemma('run')
        add_sense(run, 'noun')
        first = build_forms(run)
        self.assertEqual(WordForm.objects.filter(word=run, pos='verb', tag='verb.past', form='ran').count(), 1)
        self.assertTrue(WordForm.objects.filter(word=run, pos='noun', tag='noun.plural', form='runs').exists())
        build_forms(run)
        self.assertEqual(WordForm.objects.filter(word=run).count(), first)

    def test_resolve_form_from_table_then_library(self):
        run = self.lemma('run')
        build_forms(run)
        found = resolve_form('ran', 'en', analyze=False)
        self.assertEqual([(c.word.value, c.pos, c.tag) for c in found], [('run', 'verb', 'verb.past')])
        # chưa có trong bảng: thư viện nhận ra và từ gốc có trong DB thì ghi lại dòng đã kiểm chứng
        WordForm.objects.filter(form='ran').delete()
        self.assertEqual(resolve_form('ran', 'en', analyze=False), [])
        found = resolve_form('ran', 'en')
        self.assertEqual(found[0].word, run)
        self.assertEqual(WordForm.objects.get(form='ran', word=run).source, 'lookup')

    def test_unverified_forms_are_never_written(self):
        # từ gốc chưa có trong DB, hoặc chưa COMPLETED: không ghi gì
        self.assertEqual(resolve_form('ran', 'en'), [])
        self.lemma('run', status='PROCESSING')
        self.assertEqual(resolve_form('ran', 'en'), [])
        self.assertEqual(WordForm.objects.count(), 0)
        self.assertEqual(resolve_form('xqzvbn', 'en'), [])

    def test_form_resolves_to_every_matching_lemma(self):
        bank = self.lemma('bank', pos='noun')
        add_sense(bank, 'verb')
        build_forms(bank)
        found = resolve_form('banks', 'en', analyze=False)
        self.assertEqual({(c.pos, c.tag) for c in found}, {('noun', 'noun.plural'), ('verb', 'verb.3sg')})

    def test_forms_of_builds_when_missing(self):
        run = self.lemma('run')
        self.assertEqual(WordForm.objects.count(), 0)
        self.assertIn({'form': 'ran', 'pos': 'verb', 'tag': 'verb.past', 'irregular': True}, forms_of(run))

    def test_language_without_paradigm_builds_nothing(self):
        word = self.lemma('食べる', language='ja')
        self.assertEqual(build_forms(word), 0)
        self.assertEqual(forms_of(word), [])


@override_settings(CACHES=LOCMEM_CACHE)
class GetWordFormsApiTests(APITestCase):
    URL = '/api/ai-words/get-word/'

    def setUp(self):
        need(self, 'en', lambda: morph.analyze('ran', 'en'))
        django_cache.clear()
        self.cache = WordCacheManager()
        self.queue = FakeQueue()
        for target in ('ai.generation.push_job', 'ai.jobs.push_job', 'ai.jobs.acquire_lock'):
            patcher = mock.patch(target, getattr(self.queue, target.rsplit('.', 1)[1]))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.touched = set()

    def tearDown(self):
        for word in self.touched:
            self.cache.cache_word_clear_specific('en', word)

    def completed(self, value, pos='verb'):
        word = AIWord.objects.create(value=value, language_code='en', status='COMPLETED')
        add_sense(word, pos)
        self.touched.add(value)
        return word

    def get(self, value):
        self.touched.add(value)
        return self.client.get(self.URL, {'value': value, 'lang': 'en', 'user_lang': 'vi'})

    def test_form_without_own_entry_returns_lemma_with_form_of(self):
        self.completed('run')
        response = self.get('ran')
        self.assertEqual(response.status_code, 200)
        data = response.data['data']
        self.assertEqual(data['value'], 'run')
        self.assertEqual((data['form_of']['form'], data['form_of']['tag'], data['form_of']['own_entry']), ('ran', 'verb.past', False))
        self.assertEqual(self.queue.jobs, [])  # không tạo từ 'ran' bằng AI

    def test_lemma_lookup_has_no_form_of_and_costs_no_extra_queries(self):
        self.completed('run')
        self.assertNotIn('form_of', self.get('run').data['data'])

    def test_forms_endpoint_lists_forms_and_notes_other_lemma(self):
        see = self.completed('see')
        build_forms(see)
        saw = self.completed('saw', pos='noun')
        data = self.client.get('/api/ai-words/forms/', {'word_id': str(see.id)}).data
        self.assertIn('saw', {f['form'] for f in data['forms']})
        self.assertIsNone(data['also_form_of'])
        data = self.client.get('/api/ai-words/forms/', {'word_id': str(saw.id)}).data
        self.assertEqual((data['also_form_of']['lemma'], data['also_form_of']['tag'], data['also_form_of']['own_entry']), ('see', 'verb.past', True))

    def test_forms_endpoint_validation(self):
        self.assertEqual(self.client.get('/api/ai-words/forms/').status_code, 400)
        self.assertEqual(self.client.get('/api/ai-words/forms/', {'word_id': str(uuid7())}).status_code, 404)

    def test_unknown_string_still_goes_to_generation(self):
        response = self.get('xqzvbn')
        self.assertEqual(response.status_code, 201)
        self.assertTrue(self.queue.jobs)

    def test_form_whose_lemma_is_not_in_db_redirects_to_lemma(self):
        response = self.get('killed')
        self.assertEqual((response.status_code, response.data['detail']), (200, 'FORM_OF'))
        data = response.data['data']
        self.assertEqual(data['redirect'], 'kill')
        self.assertEqual((data['form_of']['form'], data['form_of']['lemma'], data['form_of']['tag'], data['form_of']['own_entry']),
                         ('killed', 'kill', 'verb.past', False))
        self.assertEqual(self.queue.jobs, [])  # chưa tạo gì: app tra tiếp từ gốc
        self.assertFalse(AIWord.objects.filter(value='killed').exists())  # không tạo mục riêng cho dạng chia

    def test_lemma_lookup_after_redirect_starts_generation_of_lemma(self):
        self.get('killed')
        response = self.get('kill')
        self.assertEqual(response.status_code, 201)
        self.assertEqual([payload['value'] for _, payload in self.queue.jobs], ['kill'])

    def test_comparative_with_unseeded_lemma_is_not_guessed(self):
        response = self.get('taller')  # nhãn so sánh kém tin cậy (number -> numb): vẫn đi đường cũ
        self.assertEqual(response.status_code, 201)
        self.assertEqual([payload['value'] for _, payload in self.queue.jobs], ['taller'])

    def test_rejected_lemma_is_not_redirected_to(self):
        AIWord.objects.create(value='kill', language_code='en', status='REJECTED')
        self.assertEqual(self.get('killed').status_code, 201)

    def test_language_without_reliable_analysis_is_not_guessed(self):
        self.touched.add('killed')
        response = self.client.get(self.URL, {'value': 'killed', 'lang': 'fr', 'user_lang': 'vi'})
        self.assertEqual(response.status_code, 201)
        self.cache.cache_word_clear_specific('fr', 'killed')


@override_settings(CACHES=LOCMEM_CACHE)
class CheckFormsApiTests(APITestCase):
    def setUp(self):
        need(self, 'en', lambda: morph.analyze('ran', 'en'))
        self.client.force_authenticate(__import__('django.contrib.auth', fromlist=['get_user_model']).get_user_model().objects.create_user('u'))

    def test_known_forms_count_as_existing_with_their_lemma(self):
        word = AIWord.objects.create(value='run', language_code='en', status='COMPLETED')
        add_sense(word, 'verb')
        build_forms(word)
        results = self.client.post('/api/ai-words/check/', {'lang': 'en', 'words': ['ran', 'run', 'qwzxkv']}, format='json').data['results']
        self.assertEqual(results['ran'], {'status': 'exists', 'form_of': 'run'})
        self.assertEqual(results['run'], {'status': 'exists'})
        self.assertEqual(results['qwzxkv']['status'], 'unknown')


class GuessLemmasTests(TestCase):
    def setUp(self):
        need(self, 'en', lambda: morph.analyze('ran', 'en'))

    def test_guesses_lemma_not_in_db(self):
        self.assertEqual([(g.lemma, g.pos, g.tag) for g in guess_lemmas('killed', 'en')], [('kill', 'verb', 'verb.past')])

    def test_lemma_and_nonsense_give_nothing(self):
        self.assertEqual(guess_lemmas('run', 'en'), [])
        self.assertEqual(guess_lemmas('xqzvbn', 'en'), [])

    def test_unreliable_comparative_tags_dropped(self):
        self.assertEqual(guess_lemmas('number', 'en'), [])  # numb: so sánh nhận nhầm
        self.assertEqual(guess_lemmas('fastest', 'en'), [])

    def test_banned_or_rejected_lemma_dropped(self):
        AIWord.objects.create(value='kill', language_code='en', status='BANNED')
        self.assertEqual(guess_lemmas('killed', 'en'), [])

    def test_ambiguous_form_keeps_every_lemma(self):
        self.assertEqual({g.lemma for g in guess_lemmas('lives', 'en')}, {'life', 'live'})

    def test_unsupported_language(self):
        self.assertEqual(guess_lemmas('killed', 'fr'), [])
        self.assertEqual(guess_lemmas('killed', 'vi'), [])


@override_settings(CACHES=LOCMEM_CACHE)
class BulkFormsApiTests(APITestCase):
    """bulk-create / check / senses đổi dạng chia thành từ gốc (ai/bulk.py fold_forms), không tạo mục riêng cho dạng chia."""

    def setUp(self):
        need(self, 'en', lambda: morph.analyze('ran', 'en'))
        django_cache.clear()
        self.cache = WordCacheManager()
        self.queue = FakeQueue()
        for target in ('ai.generation.push_job', 'ai.jobs.push_job', 'ai.jobs.acquire_lock'):
            patcher = mock.patch(target, getattr(self.queue, target.rsplit('.', 1)[1]))
            patcher.start()
            self.addCleanup(patcher.stop)
        from django.contrib.auth import get_user_model
        self.client.force_authenticate(get_user_model().objects.create_user('bulkform'))
        self.touched = set()

    def tearDown(self):
        for word in self.touched:
            self.cache.cache_word_clear_specific('en', word)

    def completed(self, value, pos='verb'):
        word = AIWord.objects.create(value=value, language_code='en', status='COMPLETED')
        add_sense(word, pos)
        return word

    def post(self, url, words):
        self.touched.update(words)
        return self.client.post(url, {'lang': 'en', 'user_lang': 'vi', 'words': words}, format='json')

    def test_bulk_create_starts_the_lemma_not_the_form(self):
        self.touched.add('kill')
        results = self.post('/api/ai-words/bulk-create/', ['killed']).data['results']
        self.assertEqual(results['killed'], {'status': 'processing', 'form_of': 'kill'})
        self.assertEqual([payload['value'] for _, payload in self.queue.jobs], ['kill'])
        self.assertFalse(AIWord.objects.filter(value='killed').exists())

    def test_bulk_create_form_and_lemma_together_start_one_job(self):
        self.touched.add('kill')
        results = self.post('/api/ai-words/bulk-create/', ['kill', 'killed']).data['results']
        self.assertEqual([payload['value'] for _, payload in self.queue.jobs], ['kill'])
        self.assertEqual(results['kill']['status'], 'processing')
        self.assertEqual(results['killed']['form_of'], 'kill')

    def test_check_form_of_existing_lemma_is_exists_with_form_of(self):
        self.completed('run')
        results = self.post('/api/ai-words/check/', ['ran', 'run'])
        self.assertEqual(results.data['results']['ran'], {'status': 'exists', 'form_of': 'run'})
        self.assertEqual(results.data['results']['run'], {'status': 'exists'})

    def test_form_with_own_entry_is_left_alone(self):
        self.completed('see')
        self.completed('saw', pos='noun')
        results = self.post('/api/ai-words/check/', ['saw']).data['results']
        self.assertEqual(results['saw'], {'status': 'exists'})

    def test_senses_of_a_form_are_the_lemma_senses(self):
        self.completed('run')
        response = self.client.get('/api/ai-words/senses/', {'lang': 'en', 'user_lang': 'vi', 'words': 'ran'})
        result = response.data['results']['ran']
        self.assertEqual(result['form_of'], 'run')
        self.assertTrue(result['senses'])

    def test_comparative_with_unseeded_lemma_is_not_folded(self):
        self.touched.add('taller')
        results = self.post('/api/ai-words/bulk-create/', ['taller']).data['results']
        self.assertNotIn('form_of', results['taller'])
        self.assertEqual([payload['value'] for _, payload in self.queue.jobs], ['taller'])


class PromptsForbidInflectionSensesTests(TestCase):
    """Tạo từ chỉ viết nghĩa của chính từ đó, không viết nghĩa "là dạng chia của từ khác" (bảng dạng chia lo việc đó)."""

    def test_draft_and_refine_prompts_carry_the_rule(self):
        from utils.ai.prompt import render_word_prompt
        from utils.ai.refine import render_refine_prompt
        self.assertIn('never write a sense whose only meaning is', render_word_prompt('killed', 'en'))
        self.assertIn('only meaning is an inflected form of another word', render_refine_prompt('killed', 'en', [{'id': 'x', 'pos': 'verb', 'contents': {}}]))


class FormsBuiltOnSaveTests(TestCase):
    """Bảng dạng chia dựng ngay khi nghĩa được ghi (save_sense), không đợi ai gọi ai-words/forms."""

    def setUp(self):
        need(self, 'en', lambda: morph.paradigm('run', 'verb', 'en'))

    def sense(self, word, pos):
        return {'id': str(uuid7()), 'word_id': str(word.id), 'word_value': word.value, 'language_code': 'en', 'pos': pos, 'contents': {}}

    def test_save_sense_builds_forms(self):
        from utils.ai.word_render import save_sense
        word = AIWord.objects.create(value='run', language_code='en', status='PROCESSING')
        self.assertEqual(WordForm.objects.filter(word=word).count(), 0)
        save_sense([self.sense(word, 'verb')], word.id)
        self.assertTrue({'ran', 'running', 'runs'} <= set(WordForm.objects.filter(word=word).values_list('form', flat=True)))

    def test_save_again_and_new_pos_only_add_rows(self):
        from utils.ai.word_render import save_sense
        word = AIWord.objects.create(value='run', language_code='en', status='PROCESSING')
        save_sense([self.sense(word, 'verb')], word.id)
        before = WordForm.objects.filter(word=word).count()
        save_sense([self.sense(word, 'noun')], word.id)  # thêm nghĩa danh từ: thêm dạng số nhiều
        after = set(WordForm.objects.filter(word=word).values_list('form', 'pos'))
        self.assertGreater(len(after), before)
        self.assertIn(('runs', 'noun'), after)

    def test_build_failure_never_breaks_saving(self):
        from utils.ai.word_render import save_sense
        word = AIWord.objects.create(value='run', language_code='en', status='PROCESSING')
        with mock.patch('ai.forms.build_forms', side_effect=RuntimeError('lib down')):
            save_sense([self.sense(word, 'verb')], word.id)
        word.refresh_from_db()
        self.assertEqual(word.status, 'COMPLETED')
        self.assertTrue(AISense.objects.filter(word=word).exists())
