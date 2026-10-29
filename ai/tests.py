import copy
import json
import re
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APITestCase
from uuid6 import uuid7

from ai.models import AISense, AISenseMetadata, AIWord
from utils.ai import refine
from utils.ai.word_render import ai_create_new_word_sema
from utils.ai.japanese import add_ruby
from utils.ai.schema import complex_schema
from utils.celery.worker import re_cache_word, save_translated_contents
from utils.redis.word_init import WordCacheManager, compute_languages
from utils.utils.extract_object_from_string import extract_json_object
from utils.utils.sense_handle import get_object_lang, get_user_lang_sense, merge_translations
from utils.utils.permissions import WordGenerateThrottle

User = get_user_model()

LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def make_sense(sense_id='s1', pos='noun', definition='A financial institution.', usage='', examples=('I went to the bank.',), level='A2'):
    return {
        'id': sense_id,
        'word_value': 'bank',
        'language_code': 'en',
        'pos': pos,
        'level': level,
        'register': ['neutral'],
        'ipas': [{'value': '/bæŋk/', 'label': 'US'}],
        'is_offensive': False,
        'contents': {
            'definition': {'en': {'value': definition}, 'vi': {'value': 'bản dịch cũ'}},
            'usage': {'en': {'value': usage}},
            'translations': {'vi': ['ngân hàng']},
            'examples': [{'en': {'value': e}, 'vi': {'value': 'dịch ' + e}} for e in examples],
        },
    }


class ApplyRefinementTests(TestCase):
    def apply(self, senses, items):
        return refine.apply_refinement(senses, {'senses': items}, 'en')

    def test_keep_leaves_sense_untouched(self):
        senses = [make_sense()]
        before = copy.deepcopy(senses)
        survivors, report = self.apply(senses, [{'id': 's1', 'action': 'keep'}])
        self.assertEqual(survivors, before)
        self.assertEqual(report['changed'], [])

    def test_missing_sense_is_kept(self):
        senses = [make_sense('s1'), make_sense('s2')]
        survivors, report = self.apply(senses, [{'id': 's1', 'action': 'keep'}])
        self.assertEqual(len(survivors), 2)

    def test_changed_definition_drops_stale_translations(self):
        senses = [make_sense()]
        survivors, report = self.apply(senses, [{'id': 's1', 'action': 'fix', 'definition': 'An organisation that keeps money.', 'reason': 'clearer'}])
        contents = survivors[0]['contents']
        self.assertEqual(contents['definition'], {'en': {'value': 'An organisation that keeps money.'}})
        self.assertNotIn('translations', contents)
        self.assertEqual(report['changed'][0]['fields'], ['definition'])
        self.assertTrue(report['scope']['s1']['definition'])

    def test_same_value_is_not_a_change(self):
        senses = [make_sense()]
        survivors, report = self.apply(senses, [{'id': 's1', 'action': 'fix', 'definition': 'A financial institution.', 'pos': 'noun'}])
        self.assertEqual(report['changed'], [])
        self.assertIn('vi', survivors[0]['contents']['definition'])

    def test_changed_example_keeps_translations_of_unchanged_ones(self):
        senses = [make_sense(examples=('First one.', 'Second one.'))]
        survivors, report = self.apply(senses, [{'id': 's1', 'action': 'fix', 'examples': ['First one.', 'A better second.']}])
        examples = survivors[0]['contents']['examples']
        self.assertEqual(examples[0]['vi']['value'], 'dịch First one.')
        self.assertNotIn('vi', examples[1])
        self.assertEqual(report['scope']['s1']['examples'], [1])

    def test_normalizes_and_validates_fields(self):
        senses = [make_sense(pos='Noun', level='b1')]
        survivors, report = self.apply(senses, [{
            'id': 's1', 'action': 'fix',
            'pos': ' Noun ', 'level': 'b1', 'register': ['formal', 'bogus', 'formal'],
            'ipas': [{'value': '', 'label': 'US'}, {'value': '/bank/', 'label': 'UK'}],
        }])
        sense = survivors[0]
        self.assertEqual(sense['pos'], 'noun')
        self.assertEqual(sense['level'], 'B1')
        self.assertEqual(sense['register'], ['formal'])
        self.assertEqual(sense['ipas'], [{'value': '/bank/', 'label': 'UK'}])

    def test_invalid_level_is_ignored(self):
        senses = [make_sense(level='A2')]
        survivors, _ = self.apply(senses, [{'id': 's1', 'action': 'fix', 'level': 'not a level!!'}])
        self.assertEqual(survivors[0]['level'], 'A2')

    def test_remove_and_unknown_id(self):
        senses = [make_sense('s1'), make_sense('s2')]
        survivors, report = self.apply(senses, [
            {'id': 's2', 'action': 'remove', 'reason': 'duplicate'},
            {'id': 'nope', 'action': 'remove'},
        ])
        self.assertEqual([s['id'] for s in survivors], ['s1'])
        self.assertEqual(report['removed'], [{'id': 's2', 'reason': 'duplicate'}])

    def test_never_removes_every_sense(self):
        senses = [make_sense('s1'), make_sense('s2')]
        survivors, report = self.apply(senses, [{'id': 's1', 'action': 'remove'}, {'id': 's2', 'action': 'remove'}])
        self.assertEqual(len(survivors), 2)
        self.assertTrue(report['all_removed_ignored'])

    def test_garbage_refinement_changes_nothing(self):
        senses = [make_sense()]
        for garbage in (None, {}, {'senses': 'x'}, {'senses': [None, 5, {'action': 'fix'}]}):
            survivors, report = refine.apply_refinement(senses, garbage, 'en')
            self.assertEqual(len(survivors), 1)
            self.assertEqual(report['changed'], [])


class BuildTranslateJobsTests(TestCase):
    def test_job_only_has_changed_parts(self):
        senses = [make_sense(examples=('One.', 'Two.'))]
        _, report = refine.apply_refinement(senses, {'senses': [{'id': 's1', 'action': 'fix', 'definition': 'New def.', 'examples': ['One.', 'Two changed.']}]}, 'en')
        jobs = refine.build_translate_jobs(senses, report, 'en')
        self.assertEqual(len(jobs), 1)
        job = jobs[0]['contents']
        self.assertEqual(job['definition'], {'en': {'value': 'New def.'}})
        self.assertNotIn('usage', job)
        self.assertEqual(job['examples'], [{'index': 2, 'en': {'value': 'Two changed.'}}])  # index 1-based như translate.py
        self.assertEqual(job['translations'], 'New def.')

    def test_translations_not_redone_when_definition_unchanged(self):
        senses = [make_sense()]
        _, report = refine.apply_refinement(senses, {'senses': [{'id': 's1', 'action': 'fix', 'usage': 'at the bank'}]}, 'en')
        job = refine.build_translate_jobs(senses, report, 'en')[0]['contents']
        self.assertIs(job['translations'], False)

    def test_no_job_for_non_text_changes(self):
        senses = [make_sense()]
        _, report = refine.apply_refinement(senses, {'senses': [{'id': 's1', 'action': 'fix', 'level': 'B2'}]}, 'en')
        self.assertEqual(refine.build_translate_jobs(senses, report, 'en'), [])


class ComputeLanguagesTests(TestCase):
    def test_all_translated(self):
        self.assertEqual(compute_languages([make_sense()], 'en'), ['en', 'vi'])

    def test_missing_example_translation(self):
        sense = make_sense()
        del sense['contents']['examples'][0]['vi']
        self.assertEqual(compute_languages([make_sense(), sense], 'en'), ['en'])

    def test_missing_word_translations(self):
        sense = make_sense()
        del sense['contents']['translations']
        self.assertEqual(compute_languages([sense], 'en'), ['en'])

    def test_empty_usage_needs_no_translation(self):
        self.assertIn('vi', compute_languages([make_sense(usage='')], 'en'))

    def test_usage_with_text_needs_translation(self):
        self.assertEqual(compute_languages([make_sense(usage='at the bank')], 'en'), ['en'])

    def test_no_senses(self):
        self.assertEqual(compute_languages([], 'en'), ['en'])


class ExtractJsonObjectTests(TestCase):
    def test_returns_word_metadata_not_first_sense(self):
        payload = {"word": "asdf", "metadata": {"should_be_saved": False}, "senses": [{"pos": "noun"}]}
        self.assertEqual(json.loads(extract_json_object(json.dumps(payload), 'metadata')), {"should_be_saved": False})

    def test_incomplete_stream_returns_none(self):
        text = '{"word": "a", "metadata": {"should_be_saved": tr'
        self.assertIsNone(extract_json_object(text, 'metadata'))

    def test_missing_key_returns_none(self):
        self.assertIsNone(extract_json_object('{"word": "a"}', 'metadata'))

    def test_braces_inside_strings(self):
        text = '{"metadata": {"note": "} {", "should_be_saved": true}, "senses": []}'
        self.assertEqual(json.loads(extract_json_object(text, 'metadata'))['should_be_saved'], True)


class EmptyContentTests(TestCase):
    def test_empty_source_content_needs_no_translation(self):
        self.assertEqual(get_object_lang({'en': {'value': ''}}, 'en', 'vi'), ({'en': {'value': ''}}, True))

    def test_sense_with_empty_usage_is_complete(self):
        _, missing = get_user_lang_sense('en', 'vi', make_sense(usage='')['contents'], 's1')
        self.assertFalse(missing)  # trước đây báo thiếu usage mãi mãi -> get-word luôn 206 và không bao giờ cache

    def test_non_empty_usage_without_translation_is_missing(self):
        _, missing = get_user_lang_sense('en', 'vi', make_sense(usage='at the bank')['contents'], 's1')
        self.assertIn('usage', missing['contents'])


# ======================= PIPELINE 2 BƯỚC (Gemini giả) =======================

class FakeChunk:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, script):
        self.script = script

    async def generate_content_stream(self, model, contents, config):
        pieces = self.script(model)

        async def gen():
            for piece in pieces:
                yield FakeChunk(piece)
        return gen()


class FakeAio:
    def __init__(self, models):
        self.models = models

    async def __aenter__(self):
        return SimpleNamespace(models=self.models)

    async def __aexit__(self, *args):
        return False


def fake_genai(script):
    return SimpleNamespace(Client=lambda api_key=None: SimpleNamespace(aio=FakeAio(FakeModels(script))))


def stream_pieces(payload, size=23):
    text = json.dumps(payload)
    return [text[i:i + size] for i in range(0, len(text), size)]


DRAFT = {
    "word": "zzbank",
    "metadata": {"should_be_saved": True, "is_common": True, "language_confidence": 0.99},
    "senses": [
        {"pos": "noun", "definition": {"value": "A financial institution."}, "usage": {"value": "at the bank"},
         "examples": [{"value": "I went to the zzbank."}], "is_offensive": False, "level": "A2",
         "register": ["neutral"], "ipas": [{"value": "/bæŋk/", "label": "US"}]},
        {"pos": "Noun", "definition": {"value": "Land beside a river."}, "usage": {"value": ""},
         "examples": [{"value": "We sat on the zzbank."}], "is_offensive": False, "level": "b1",
         "register": ["neutral"], "ipas": [{"value": "/bæŋk/", "label": "US"}]},
    ],
}


class PipelineTests(TestCase):
    """Chạy ai_create_new_word_sema thật (stream, cache Redis, ghi Postgres); chỉ Gemini, dịch, metadata, socket là giả."""

    WORD = 'zzbank'

    def setUp(self):
        self.cache = WordCacheManager()
        self.translate_calls = []
        self.metadata_calls = []
        self.refine_prompts = []
        self.prepare(self.WORD, 'en')

    def prepare(self, word, lang):
        """Giống lúc view vừa nhận việc tạo từ: cache PROCESSING + bản ghi AIWord + job trong hàng đợi."""
        self.cache.cache_word_clear_specific(lang, word)
        _, init = self.cache.cache_word_init(lang, word, 'vi')
        self.word = AIWord.objects.create(id=init['word']['id'], value=word, language_code=lang, status='PROCESSING')
        self.job = {'word_id': str(self.word.id), 'value': word, 'language_code': lang, 'user_language_code': 'vi'}
        self.addCleanup(self.cache.cache_word_clear_specific, lang, word)

    async def fake_translate(self, props, translate_base_language=True):
        self.translate_calls.append(copy.deepcopy(props['missing_translate']))
        src = props['language_code']
        jobs = {j['id']: j.get('contents', j) for j in props['missing_translate']}
        for sense in props['current_senses']:
            job, contents = jobs[sense['id']], sense['contents']
            if 'definition' in job:
                contents['definition']['vi'] = {'value': 'vi: ' + contents['definition'][src]['value']}
            if 'usage' in job and contents['usage'][src]['value']:
                contents['usage']['vi'] = {'value': 'vi: ' + contents['usage'][src]['value']}
            for i, example in enumerate(job.get('examples', []), 1):
                target = contents['examples'][example.get('index', i) - 1]
                target['vi'] = {'value': 'vi: ' + target[src]['value']}
            if job.get('translations') is not False:
                contents['translations'] = {'vi': ['vi-word']}
        return props['current_senses']

    async def fake_metadata(self, props):
        self.metadata_calls.append([s['id'] for s in props['current_senses']])
        for sense in props['current_senses']:
            sense['metadata'] = {'id': str(uuid7()), 'tags': ['t'], 'image_keywords': [], 'advanced': {}}
        return props['current_senses']

    def refine_with(self, builder):
        async def fake_request(prompt):
            self.refine_prompts.append(prompt)
            ids = re.findall(r'"id": "([0-9a-f-]{36})"', prompt)
            return {'data': {'senses': builder(ids)}, 'model': 'fake-pro'}
        return fake_request

    def run_pipeline(self, script, request_refinement, metadata=None):
        patches = [
            mock.patch('utils.ai.word_render.genai', fake_genai(script)),
            mock.patch('utils.ai.word_render.ai_create_translate_sema', self.fake_translate),
            mock.patch('utils.ai.word_render.ai_create_metadata', metadata or self.fake_metadata),
            mock.patch('utils.ai.translate.ai_create_translate_sema', self.fake_translate),
            mock.patch('utils.ai.metadata.ai_create_metadata', metadata or self.fake_metadata),
            mock.patch('utils.ai.word_render.request_refinement', request_refinement),
            mock.patch('utils.ai.word_render.socket_message', AsyncMock()),
            mock.patch('utils.ai.refine.socket_message', AsyncMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return async_to_sync(ai_create_new_word_sema)(self.job)

    def senses(self):
        return list(AISense.objects.filter(word=self.word).order_by('id'))

    def test_refined_data_is_what_gets_saved(self):
        socket = AsyncMock()

        def builder(ids):
            return [
                {'id': ids[0], 'action': 'fix', 'definition': 'An organisation that keeps money safe.', 'reason': 'clearer'},
                {'id': ids[1], 'action': 'fix', 'pos': 'noun', 'level': 'B1'},
            ]

        with mock.patch('utils.ai.refine.socket_message', socket):
            result = self.run_pipeline(lambda m: stream_pieces(DRAFT), self.refine_with(builder))
        self.assertTrue(result['is_valid'])

        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'COMPLETED')
        first, second = self.senses()

        # Bản chuẩn hoá được lưu, không phải draft
        self.assertEqual(first.contents['definition']['en']['value'], 'An organisation that keeps money safe.')
        self.assertEqual((second.pos, second.level), ('noun', 'B1'))
        self.assertEqual((first.quality_status, first.ai_pass, first.ai_model), ('refined', 2, 'fake-pro'))
        self.assertEqual(first.ai_review['fields'], ['definition'])

        # Bản dịch cũ của draft đã bị bỏ và dịch lại từ nghĩa mới
        self.assertEqual(first.contents['definition']['vi']['value'], 'vi: An organisation that keeps money safe.')
        self.assertEqual(first.contents['translations'], {'vi': ['vi-word']})
        self.assertEqual(self.translate_calls[-1][0]['contents'].keys() - {'translations'}, {'definition'})

        # Metadata sinh lại cho nghĩa đổi nghĩa / loại từ, và được lưu cùng nghĩa
        self.assertEqual(sorted(self.metadata_calls[-1]), sorted([str(first.id), str(second.id)]))
        self.assertEqual(AISenseMetadata.objects.count(), 2)
        self.assertIsNotNone(first.metadata_id)

        # Cache của lần tra tiếp theo là bản chuẩn hoá
        cached = self.cache.cache_word_get_data('en', self.WORD)
        self.assertEqual(cached['status'], 'CACHED')
        self.assertIn('vi', cached['languages'])
        self.assertEqual(len(cached['senses']), 2)

    def test_refine_prompt_only_has_source_language_content(self):
        self.run_pipeline(lambda m: stream_pieces(DRAFT), self.refine_with(lambda ids: []))
        prompt = self.refine_prompts[0]
        self.assertIn('A financial institution.', prompt)
        self.assertNotIn('vi:', prompt)
        self.assertNotIn('ngân hàng', prompt)

    def test_removed_sense_never_reaches_db_or_cache(self):
        def builder(ids):
            return [{'id': ids[1], 'action': 'remove', 'reason': 'duplicate'}]

        self.run_pipeline(lambda m: stream_pieces(DRAFT), self.refine_with(builder))
        self.assertEqual(len(self.senses()), 1)
        self.assertEqual(len(self.cache.cache_word_get_data('en', self.WORD)['senses']), 1)

    def test_refine_failure_keeps_draft(self):
        result = self.run_pipeline(lambda m: stream_pieces(DRAFT), AsyncMock(return_value=None))
        self.assertTrue(result['is_valid'])
        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'COMPLETED')
        for sense in self.senses():
            self.assertEqual((sense.quality_status, sense.ai_pass), ('draft', 1))
            self.assertTrue(sense.ai_model)

    @override_settings(AI_REFINE_ENABLED=False)
    def test_refine_can_be_switched_off(self):
        request = AsyncMock(return_value=None)
        self.run_pipeline(lambda m: stream_pieces(DRAFT), request)
        request.assert_not_called()
        self.assertEqual({s.quality_status for s in self.senses()}, {'draft'})

    def test_sense_without_metadata_is_still_saved(self):
        async def no_metadata(props):
            return None

        self.run_pipeline(lambda m: stream_pieces(DRAFT), self.refine_with(lambda ids: []), metadata=no_metadata)
        self.assertEqual(len(self.senses()), 2)
        self.assertTrue(all(s.metadata_id is None for s in self.senses()))

    def test_rejected_word_is_not_saved_or_cached_as_complete(self):
        rejected = {"word": "zzbank", "metadata": {"should_be_saved": False, "is_common": False, "language_confidence": 0.1}, "senses": []}
        request = AsyncMock()
        result = self.run_pipeline(lambda m: stream_pieces(rejected), request)
        self.assertFalse(result['is_valid'])
        request.assert_not_called()
        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'REJECTED')  # trước đây bị ghi đè thành COMPLETED với 0 nghĩa
        self.assertEqual(self.senses(), [])
        self.assertEqual(self.cache.cache_word_get_data('en', self.WORD)['status'], 'NOT_FOUND')

    def test_rejected_word_with_senses_is_still_rejected(self):
        # Model hay trả vài nghĩa "cho có" kèm should_be_saved=false: trước đây đọc nhầm metadata từ nghĩa đầu tiên nên không bao giờ từ chối
        rejected = {**DRAFT, "metadata": {"should_be_saved": False, "is_common": False, "language_confidence": 0.1}}
        request = AsyncMock()
        self.run_pipeline(lambda m: stream_pieces(rejected), request)
        request.assert_not_called()
        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'REJECTED')
        self.assertEqual(self.senses(), [])

    def test_japanese_word_uses_same_flow_and_gets_ruby(self):
        self.prepare('猫', 'ja')
        result = self.run_pipeline(lambda m: stream_pieces(JA_DRAFT), self.refine_with(lambda ids: []))
        self.assertTrue(result['is_valid'])

        sense = self.senses()[0]
        self.assertEqual((sense.pos, sense.level), ('noun', 'N5'))
        self.assertEqual(sense.ipas[0]['roman'], 'neko')
        definition = sense.contents['definition']['ja']
        self.assertEqual(definition['roman'], 'chiisakute kawaii doubutsu.')
        # Furigana do server sinh: bỏ markup đi phải ra đúng văn bản gốc
        self.assertEqual(re.sub(r'\[([^|\]]+)\|[^\]]+\]', r'\1', definition['ruby']), definition['value'])
        self.assertEqual(sense.contents['examples'][0]['ja']['ruby'], '[猫|ねこ]がいる。')
        # Dịch và metadata chạy được vì nội dung nằm ở 'value' như tiếng Latin
        self.assertEqual(sense.contents['definition']['vi']['value'], 'vi: ' + definition['value'])
        self.assertIsNotNone(sense.metadata_id)
        self.assertNotIn('[猫|', self.refine_prompts[0])  # refine không thấy markup furigana

    def test_japanese_refine_regenerates_ruby_and_drops_stale_romaji(self):
        self.prepare('猫', 'ja')

        def builder(ids):
            return [{'id': ids[0], 'action': 'fix', 'definition': '小さい動物。'}]

        self.run_pipeline(lambda m: stream_pieces(JA_DRAFT), self.refine_with(builder))
        sense = self.senses()[0]
        definition = sense.contents['definition']['ja']
        self.assertEqual(definition['value'], '小さい動物。')
        self.assertIn('|', definition['ruby'])
        self.assertNotIn('roman', definition)  # AI refine chỉ trả văn bản nên romaji cũ của đoạn đã đổi không còn đúng
        example = sense.contents['examples'][0]['ja']
        self.assertEqual((example['roman'], example['ruby']), ('neko ga iru.', '[猫|ねこ]がいる。'))

    def test_no_ai_response_marks_word_failed(self):
        result = self.run_pipeline(lambda m: [], AsyncMock())
        self.assertFalse(result['is_valid'])
        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'FAILED')
        self.assertEqual(self.cache.cache_word_get_data('en', self.WORD)['status'], 'ERROR')


# ======================= API get-word =======================

JA_DRAFT = {
    "word": "猫",
    "metadata": {"should_be_saved": True, "is_common": True, "language_confidence": 0.99},
    "senses": [{
        "pos": "noun",
        "definition": {"value": "小さくてかわいい動物。", "roman": "chiisakute kawaii doubutsu."},
        "usage": {"value": "", "roman": ""},
        "examples": [{"value": "猫がいる。", "roman": "neko ga iru."}],
        "is_offensive": False, "level": "N5", "register": ["neutral"],
        "ipas": [{"value": "ねこ", "label": "READING", "roman": "neko"}],
    }],
}


class JapaneseSchemaTests(TestCase):
    def test_schema_is_flat_like_latin_plus_roman(self):
        props = complex_schema["properties"]
        self.assertNotIn("entries", props)
        sense = props["senses"]["items"]["properties"]
        self.assertEqual(sorted(sense["definition"]["properties"]), ["roman", "value"])
        self.assertEqual(sorted(sense["examples"]["items"]["properties"]), ["roman", "value"])
        self.assertIn("roman", sense["ipas"]["items"]["properties"])
        self.assertIn("pos", sense)  # pos / level / ipas nằm ở nghĩa như tiếng Latin, không còn chui vào metadata

    def test_latin_schema_is_not_modified(self):
        from utils.ai.schema import word_schema
        self.assertNotIn("roman", word_schema["properties"]["senses"]["items"]["properties"]["definition"]["properties"])

    def test_add_ruby_skips_empty_and_other_languages(self):
        contents = {'definition': {'ja': {'value': '猫'}, 'vi': {'value': 'mèo'}}, 'usage': {'ja': {'value': ''}}, 'examples': []}
        add_ruby(contents, 'ja')
        self.assertEqual(contents['definition']['ja']['ruby'], '[猫|ねこ]')
        self.assertNotIn('ruby', contents['definition']['vi'])
        self.assertNotIn('ruby', contents['usage']['ja'])


class TranslateFlowTests(TestCase):
    def test_merge_keeps_other_languages_and_source(self):
        base = make_sense()['contents']
        new = copy.deepcopy(base)
        new['definition']['en'] = {'value': 'ghi đè gốc?'}
        new['definition']['fr'] = {'value': 'fr def'}
        new['translations'] = {'fr': ['banque']}
        new['examples'][0]['fr'] = {'value': 'fr ex'}
        merged = merge_translations(base, new, 'en')
        self.assertEqual(merged['definition']['en']['value'], 'A financial institution.')
        self.assertEqual(merged['definition']['vi']['value'], 'bản dịch cũ')
        self.assertEqual(merged['definition']['fr']['value'], 'fr def')
        self.assertEqual(merged['translations'], {'vi': ['ngân hàng'], 'fr': ['banque']})
        self.assertEqual(merged['examples'][0]['fr']['value'], 'fr ex')

    def test_merge_ignores_extra_examples(self):
        base = make_sense()['contents']
        new = copy.deepcopy(base)
        new['examples'].append({'en': {'value': 'extra'}, 'fr': {'value': 'x'}})
        self.assertEqual(len(merge_translations(base, new, 'en')['examples']), 1)

    def _word_with_sense(self, word):
        db_word = AIWord.objects.create(value=word, language_code='en', status='COMPLETED')
        sense = make_sense(str(uuid7()))
        sense.update(word_id=db_word.id, word_value=word)
        row = AISense.objects.create(**sense)
        return db_word, row, sense

    def test_two_translation_jobs_do_not_overwrite_each_other(self):
        _, row, sense = self._word_with_sense('zzmerge')  # DB đã có vi do job khác vừa lưu
        stale = copy.deepcopy(sense['contents'])           # job dịch fr chụp contents trước khi vi được lưu
        stale['definition'].pop('vi')
        stale.pop('translations')
        stale['examples'][0].pop('vi')
        stale['definition']['fr'] = {'value': 'fr def'}
        stale['translations'] = {'fr': ['banque']}

        self.assertEqual(save_translated_contents([{'id': str(row.id), 'contents': stale}], 'en'), 1)
        row.refresh_from_db()
        self.assertEqual(row.contents['definition']['vi']['value'], 'bản dịch cũ')
        self.assertEqual(row.contents['definition']['fr']['value'], 'fr def')
        self.assertEqual(sorted(row.contents['translations']), ['fr', 'vi'])

    def test_recache_after_translation_reflects_db(self):
        cache = WordCacheManager()
        db_word, row, sense = self._word_with_sense('zzrecache')
        self.addCleanup(cache.cache_word_clear_specific, 'en', 'zzrecache')
        cache.cache_word_clear_specific('en', 'zzrecache')

        # Id lấy từ job (str), như worker nhận; trước đây danh sách id lấy từ sense.get('word_id') nên toàn None và không nạp gì
        re_cache_word(str(db_word.id))
        cached = cache.cache_word_get_data('en', 'zzrecache')
        self.assertEqual(cached['status'], 'CACHED')
        self.assertIn('vi', cached['languages'])
        self.assertEqual(len(cached['senses']), 1)


class FakeQueue:
    """Thay hàng đợi Redis thật: không đẩy job vào queue mà ai_worker của môi trường dev có thể đang đọc."""

    def __init__(self):
        self.jobs = []
        self.locks = set()

    def push_job(self, queue, payload):
        self.jobs.append((queue, payload))

    def acquire_lock(self, name, ttl):
        if name in self.locks:
            return False
        self.locks.add(name)
        return True

    def names(self):
        return [q for q, _ in self.jobs]


@override_settings(CACHES=LOCMEM_CACHE)
class GetWordApiTests(APITestCase):
    URL = '/api/ai-words/get-word/'

    def setUp(self):
        django_cache.clear()
        self.cache = WordCacheManager()
        self.queue = FakeQueue()
        for target in ('ai.generation.push_job', 'ai.jobs.push_job', 'ai.jobs.acquire_lock'):
            patcher = mock.patch(target, getattr(self.queue, target.rsplit('.', 1)[1]))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.words = set()

    def tearDown(self):
        for word in self.words:
            self.cache.cache_word_clear_specific('en', word)

    def get(self, word, user_lang='vi'):
        self.words.add(word)
        return self.client.get(self.URL, {'value': word, 'lang': 'en', 'user_lang': user_lang})

    def make_completed(self, word, with_vi=True):
        self.words.add(word)
        w = AIWord.objects.create(value=word, language_code='en', status='COMPLETED')
        sense = make_sense(str(uuid7()), usage='')
        sense.update(word_id=w.id, word_value=word)
        if not with_vi:
            for key in ('definition',):
                sense['contents'][key].pop('vi')
            sense['contents']['translations'] = {}
            sense['contents']['examples'][0].pop('vi')
        metadata = AISenseMetadata.objects.create(tags=['t'])
        AISense.objects.create(metadata=metadata, **sense)
        return w

    def test_completed_word_comes_from_db_with_two_queries_then_from_cache(self):
        self.make_completed('zzfull')
        with self.assertNumQueries(2):  # từ + nghĩa kèm metadata, không N+1
            res = self.get('zzfull')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['detail'], 'DATABASE_DATA')
        self.assertEqual(len(res.data['data']['senses']), 1)

        with self.assertNumQueries(0):
            res = self.get('zzfull')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['detail'], 'CACHED')
        self.assertIn('vi', self.cache.cache_word_get_data('en', 'zzfull')['languages'])

    def test_cached_word_with_other_language_is_partial_and_translation_queued_once(self):
        self.make_completed('zzpartial')
        self.assertEqual(self.get('zzpartial').status_code, 200)  # nạp cache với languages = [en, vi]

        first = self.get('zzpartial', user_lang='fr')
        second = self.get('zzpartial', user_lang='fr')
        self.assertEqual((first.status_code, second.status_code), (206, 206))
        self.assertEqual(self.queue.names(), ['redis_trans'])  # lần 2 không đẩy job dịch trùng
        self.assertEqual(self.queue.jobs[0][1]['user_language_code'], 'fr')

    def test_db_word_missing_translation_is_206(self):
        self.make_completed('zzmissing', with_vi=False)
        res = self.get('zzmissing')
        self.assertEqual(res.status_code, 206)
        self.assertEqual(self.queue.names(), ['redis_trans'])
        self.assertIsNone(self.cache.cache_word_get_data('en', 'zzmissing'))  # chưa đủ thì chưa cache

    def test_rejected_word_is_404_without_ai(self):
        AIWord.objects.create(value='zzreject', language_code='en', status='REJECTED')
        res = self.get('zzreject')
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.queue.jobs, [])

    def test_new_word_queues_one_job_and_later_requests_join(self):
        res = self.get('zznew')
        self.assertEqual(res.status_code, 201)
        word = AIWord.objects.get(value='zznew')
        self.assertEqual(word.status, 'PROCESSING')
        self.assertEqual(self.queue.names(), ['redis_word'])
        self.assertEqual(self.queue.jobs[0][1]['word_id'], str(word.id))
        self.assertEqual(res.data['data']['id'], str(word.id))

        self.assertEqual(self.get('zznew').status_code, 202)
        self.assertEqual(self.get('zznew', user_lang='fr').status_code, 202)
        self.assertEqual(self.queue.names(), ['redis_word'])  # vẫn đúng 1 job
        self.assertIn('fr', self.cache.cache_word_get_data('en', 'zznew')['langs'])  # ngôn ngữ người sau được đăng ký

    def test_failed_word_is_reused_not_duplicated(self):
        existing = AIWord.objects.create(value='zzfailed', language_code='en', status='FAILED')
        res = self.get('zzfailed')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(AIWord.objects.filter(value='zzfailed').count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.status, 'PROCESSING')
        self.assertEqual(self.queue.jobs[0][1]['word_id'], str(existing.id))
        self.assertEqual(res.data['data']['id'], str(existing.id))

    def test_fresh_processing_word_without_cache_just_waits(self):
        word = AIWord.objects.create(value='zzbusy', language_code='en', status='PROCESSING')
        self.words.add('zzbusy')
        res = self.get('zzbusy')
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data['data']['word']['id'] if 'word' in res.data['data'] else res.data['data']['id'], str(word.id))
        self.assertEqual(self.queue.jobs, [])

    def test_stale_processing_word_is_regenerated(self):
        word = AIWord.objects.create(value='zzstale', language_code='en', status='PROCESSING')
        AIWord.objects.filter(id=word.id).update(updated_at=timezone.now() - timedelta(hours=1))
        res = self.get('zzstale')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.queue.jobs[0][1]['word_id'], str(word.id))

    def test_dead_job_cache_entry_does_not_block_forever(self):
        self.words.add('zzdead')
        self.cache.cache_word_init('en', 'zzdead', 'vi')
        self.cache.r.json().set(self.cache._key('en', 'zzdead'), '.started_at', 1)  # job bắt đầu từ rất lâu
        res = self.get('zzdead')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.queue.names(), ['redis_word'])

    def test_new_word_throttled(self):
        with mock.patch.dict(WordGenerateThrottle.THROTTLE_RATES, {'word_generate': '1/hour'}):
            self.assertEqual(self.get('zzone').status_code, 201)
            res = self.get('zztwo')
        self.assertEqual(res.status_code, 429)
        self.assertFalse(AIWord.objects.filter(value='zztwo').exists())
        self.assertIsNone(self.cache.cache_word_get_data('en', 'zztwo'))  # không để lại key PROCESSING không có job
        self.assertEqual(self.queue.names(), ['redis_word'])

    def test_queue_failure_does_not_leave_word_stuck(self):
        def boom(queue, payload):
            raise ConnectionError('redis down')

        with mock.patch('ai.generation.push_job', boom):
            res = self.get('zzdown')
        self.assertEqual(res.status_code, 503)
        self.assertEqual(AIWord.objects.get(value='zzdown').status, 'FAILED')
        self.assertIsNone(self.cache.cache_word_get_data('en', 'zzdown'))
