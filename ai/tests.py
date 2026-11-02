import time
import asyncio
import copy
import json
import re
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient, APITestCase
from uuid6 import uuid7

from ai.models import AISense, AISenseMetadata, AIWord
from ai.queries import load_word
from ai.validators import is_uuid
from core.models import ImageContext, ImageLibrary
from utils.celery.fetch_image import find_image, get_image_by_keyword
from utils.ai import refine
from utils.ai.word_render import ai_create_new_word_sema
from utils.ai.japanese import add_ruby
from utils.ai.translate import ai_create_translate_sema
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

    def add_item(self, **overrides):
        return {'action': 'add', 'pos': 'Verb', 'definition': 'To deposit money in a bank.', 'examples': ['I bank at the corner branch.'],
                'level': 'b1', 'reason': 'common verb sense is missing', **overrides}

    def test_add_is_off_by_default(self):
        # Nghĩa thêm ở bước 2 không có bước nào kiểm lại nên mặc định tắt: model có trả add thì bỏ qua, prompt cũng không gợi ý
        self.assertFalse(settings.AI_REFINE_ALLOW_ADD)
        senses = [make_sense('s1')]
        survivors, report = self.apply(senses, [{'id': 's1', 'action': 'keep'}, self.add_item()])
        self.assertEqual((len(survivors), report['added']), (1, []))
        prompt = refine.render_refine_prompt('bank', 'en', senses)
        self.assertIn('Never add new senses', prompt)
        self.assertNotIn('action=add', prompt)

    @override_settings(AI_REFINE_ALLOW_ADD=True)
    def test_prompt_allows_adding_only_when_switched_on(self):
        prompt = refine.render_refine_prompt('bank', 'en', [make_sense('s1')])
        self.assertIn('action=add', prompt)
        self.assertNotIn('Never add new senses', prompt)

    @override_settings(AI_REFINE_ALLOW_ADD=True)
    def test_add_builds_a_new_sense_like_a_draft_one(self):
        senses = [make_sense('s1')]
        survivors, report = self.apply(senses, [{'id': 's1', 'action': 'keep'}, self.add_item(usage="Used with 'at'.")])
        self.assertEqual(len(survivors), 2)
        new = survivors[-1]
        self.assertEqual([a['id'] for a in report['added']], [new['id']])
        self.assertEqual((new['pos'], new['level'], new['word_value'], new['language_code'], new['register']), ('verb', 'B1', 'bank', 'en', ['neutral']))
        self.assertEqual(new['ipas'], senses[0]['ipas'])  # cùng từ nên mượn phát âm của nghĩa có sẵn
        self.assertEqual(new['contents']['definition'], {'en': {'value': 'To deposit money in a bank.'}})
        self.assertEqual(new['contents']['examples'], [{'en': {'value': 'I bank at the corner branch.'}}])
        self.assertEqual(new['contents']['usage'], {'en': {'value': "Used with 'at'."}})
        self.assertTrue(is_uuid(new['id']))

        # Nghĩa mới chưa có bản dịch nào: job dịch đủ definition, usage, example và translations
        job = refine.build_translate_jobs(survivors, report, 'en')[0]
        self.assertEqual(job['id'], new['id'])
        self.assertEqual(set(job['contents']), {'definition', 'usage', 'examples', 'translations'})
        self.assertEqual(job['contents']['translations'], 'To deposit money in a bank.')

        refine.stamp_senses(survivors, report, 'fake-pro')
        self.assertEqual(new['ai_review']['added'], True)
        self.assertEqual(survivors[0]['ai_review']['unchanged'], True)

    @override_settings(AI_REFINE_ALLOW_ADD=True)
    def test_invalid_or_duplicate_add_is_ignored(self):
        senses = [make_sense('s1', pos='noun', definition='A financial institution.')]
        for bad in (self.add_item(pos=''), self.add_item(definition=''), self.add_item(examples=[]), self.add_item(examples='x'),
                    self.add_item(pos='noun', definition=' A  financial institution. ')):
            survivors, report = self.apply(senses, [bad])
            self.assertEqual((len(survivors), report['added']), (1, []), bad)

    @override_settings(AI_REFINE_ALLOW_ADD=True, AI_REFINE_MAX_ADD=2)
    def test_add_is_capped(self):
        senses = [make_sense('s1')]
        items = [self.add_item(definition=f'meaning {i}') for i in range(5)]
        survivors, report = self.apply(senses, items)
        self.assertEqual(len(report['added']), 2)
        self.assertEqual(len(survivors), 3)

    @override_settings(AI_REFINE_ALLOW_ADD=True)
    def test_replacing_every_sense_is_allowed_when_new_ones_are_added(self):
        senses = [make_sense('s1')]
        survivors, report = self.apply(senses, [{'id': 's1', 'action': 'remove', 'reason': 'wrong'}, self.add_item()])
        self.assertEqual([s['pos'] for s in survivors], ['verb'])
        self.assertFalse(report['all_removed_ignored'])

    @override_settings(AI_REFINE_ALLOW_ADD=True)
    def test_add_for_japanese_gets_ruby(self):
        senses = [make_sense('s1')]
        senses[0]['language_code'] = 'ja'
        survivors, report = refine.apply_refinement(senses, {'senses': [self.add_item(definition='猫がいる。', examples=['猫が好きです。'])]}, 'ja')
        new = survivors[-1]
        self.assertEqual([a['id'] for a in report['added']], [new['id']])
        self.assertEqual(new['contents']['examples'][0]['ja']['ruby'], '[猫|ねこ]が[好|す]きです。')

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

    def test_empty_usage_removes_it_with_its_translations(self):
        # Draft hay nhét câu ví dụ vào usage; refine trả "" nghĩa là bỏ hẳn (không đòi dịch lại gì)
        senses = [make_sense(usage='Turn on the light.')]
        senses[0]['contents']['usage']['vi'] = {'value': 'Bật đèn.'}
        _, report = refine.apply_refinement(senses, {'senses': [{'id': 's1', 'action': 'fix', 'usage': '', 'reason': 'not a pattern'}]}, 'en')
        self.assertNotIn('usage', senses[0]['contents'])
        self.assertEqual([c['fields'] for c in report['changed']], [['usage']])
        self.assertEqual(refine.build_translate_jobs(senses, report, 'en'), [])

        # Không có usage sẵn mà model trả "" thì không phải thay đổi
        _, report = refine.apply_refinement(senses, {'senses': [{'id': 's1', 'action': 'fix', 'usage': ''}]}, 'en')
        self.assertEqual(report['changed'], [])

    def test_cosmetic_differences_are_not_a_change(self):
        # Gemini thật từng "sửa" definition thành đúng chữ cũ (khác dạng chữ / khoảng trắng): không được bỏ bản dịch rồi dịch lại
        senses = [make_sense(definition='A financial institution.', examples=('I went to the bank.',))]
        item = {'id': 's1', 'action': 'fix', 'definition': ' A  financial institution. ', 'examples': ['I went to the  bank.']}
        _, report = refine.apply_refinement(senses, {'senses': [item]}, 'en')
        self.assertEqual(report['changed'], [])
        self.assertIn('translations', senses[0]['contents'])
        self.assertEqual(refine.build_translate_jobs(senses, report, 'en'), [])

        # Full-width và half-width của cùng một chữ cũng coi là một
        self.assertTrue(refine._same('ＡＢＣ　ｄｅｆ', 'ABC def'))
        self.assertFalse(refine._same('bank', 'banks'))

    def test_no_job_for_non_text_changes(self):
        senses = [make_sense()]
        _, report = refine.apply_refinement(senses, {'senses': [{'id': 's1', 'action': 'fix', 'level': 'B2'}]}, 'en')
        self.assertEqual(refine.build_translate_jobs(senses, report, 'en'), [])


class AddRubyTests(TestCase):
    def test_skips_text_without_japanese_characters(self):
        # Draft của từ tiếng Nhật đôi khi viết usage bằng tiếng Anh: không có gì để đọc, không được sinh ruby (bộ sinh furigana lỗi với chuỗi đó)
        contents = {
            'definition': {'ja': {'value': '猫'}},
            'usage': {'ja': {'value': 'Generally used for the animal.'}},
            'examples': [{'ja': {'value': '猫がいる。'}}],
        }
        with self.assertNoLogs('utils.ai.japanese', level='WARNING'):
            add_ruby(contents, 'ja')
        self.assertIn('ruby', contents['definition']['ja'])
        self.assertNotIn('ruby', contents['usage']['ja'])
        self.assertEqual(contents['examples'][0]['ja']['ruby'], '[猫|ねこ]がいる。')


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


# ======================= PIPELINE 2 BƯỚC (Claude giả) =======================

class FakeStream:
    def __init__(self, pieces):
        self.pieces = pieces

    async def __aenter__(self):
        async def gen():
            for piece in self.pieces:
                yield piece
        return SimpleNamespace(text_stream=gen())

    async def __aexit__(self, *args):
        return False


class FakeClaude:
    """Giả AsyncAnthropic: messages.stream(model=...) trả từng mẩu text theo script(model)."""
    def __init__(self, script):
        self.messages = SimpleNamespace(stream=lambda model, **kwargs: FakeStream(script(model)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def fake_claude(script):
    return lambda: FakeClaude(script)


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


class PipelineBase(TestCase):
    """Dựng 1 từ đang PROCESSING + cache Redis, dịch / metadata giả; dùng chung cho cả 2 luồng tạo từ."""

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

    def senses(self):
        return list(AISense.objects.filter(word=self.word).order_by('id'))


class PipelineTests(PipelineBase):
    """Luồng cũ (draft -> refine): chạy ai_create_new_word_sema thật (stream, cache Redis, ghi Postgres); chỉ Claude, dịch, metadata, socket là giả."""

    def refine_with(self, builder):
        async def fake_request(prompt):
            self.refine_prompts.append(prompt)
            ids = re.findall(r'"id": "([0-9a-f-]{36})"', prompt)
            return {'data': {'senses': builder(ids)}, 'model': 'fake-pro'}
        return fake_request

    def run_pipeline(self, script, request_refinement, metadata=None, socket=None):
        patches = [
            mock.patch('utils.ai.claude.client', fake_claude(script)),
            mock.patch('utils.ai.word_render.ai_create_translate_sema', self.fake_translate),
            mock.patch('utils.ai.word_render.ai_create_metadata', metadata or self.fake_metadata),
            mock.patch('utils.ai.translate.ai_create_translate_sema', self.fake_translate),
            mock.patch('utils.ai.metadata.ai_create_metadata', metadata or self.fake_metadata),
            mock.patch('utils.ai.word_render.request_refinement', request_refinement),
            mock.patch('utils.ai.word_render.socket_message', socket or AsyncMock()),
            mock.patch('utils.ai.refine.socket_message', AsyncMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return async_to_sync(ai_create_new_word_sema)(self.job)


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

    def test_word_pronunciation_comes_from_senses(self):
        socket = AsyncMock()
        draft = copy.deepcopy(DRAFT)
        draft['senses'][1]['ipas'] = [{'value': '/bæŋk/', 'label': 'UK'}]
        draft['senses'].append({**draft['senses'][0], 'definition': {'value': 'A row of similar things.'}, 'examples': [{'value': 'A zzbank of lights.'}]})
        self.run_pipeline(lambda m: stream_pieces(draft), self.refine_with(lambda ids: []), socket=socket)

        # Người tra đầu tiên có id từ ngay khi biết từ hợp lệ, trước nghĩa đầu tiên
        events = [c.args[1] for c in socket.call_args_list]
        types = [e['type'] for e in events]
        self.assertLess(types.index('WORD'), types.index('PARTIAL_SENSE'))
        self.assertEqual(events[types.index('WORD')]['payload']['id'], str(self.word.id))

        # Nghĩa giữ phát âm riêng; phát âm của từ = cái hay gặp nhất
        us = [{'value': '/bæŋk/', 'label': 'US'}]
        self.assertEqual([s.ipas for s in self.senses()], [us, [{'value': '/bæŋk/', 'label': 'UK'}], us])
        self.word.refresh_from_db()
        self.assertEqual((self.word.ipas, self.word.audio), (us, None))
        full = events[types.index('FULL_SENSE')]['payload']
        self.assertEqual((full['ipas'], full['audio']), (us, None))
        self.assertIn('audio', next(iter(full['senses'])))
        cached = self.cache.cache_word_get_data('en', self.WORD)['word']
        self.assertEqual((cached['ipas'], cached['audio']), (us, None))

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

    def test_draft_never_reaches_cache_and_stage_is_visible(self):
        self.assertEqual(self.cache.cache_word_get_data('en', self.WORD)['stage'], 'draft')
        seen = {}

        async def request(prompt):
            seen.update(self.cache.cache_word_get_data('en', self.WORD))  # người tra khác đang xem cache đúng lúc bước 2 chạy
            ids = re.findall(r'"id": "([0-9a-f-]{36})"', prompt)
            return {'data': {'senses': [{'id': i, 'action': 'keep'} for i in ids]}, 'model': 'fake-pro'}

        self.run_pipeline(lambda m: stream_pieces(DRAFT), request)
        self.assertEqual((seen['status'], seen['stage']), ('PROCESSING', 'refine'))
        self.assertEqual(seen['senses'], {})  # nháp bước 1 chỉ đi qua socket, không nằm trong Redis

        final = self.cache.cache_word_get_data('en', self.WORD)
        self.assertEqual(final['status'], 'CACHED')
        self.assertNotIn('stage', final)
        self.assertEqual(set(final['senses']), {str(s.id) for s in self.senses()})

    async def metadata_with_keywords(self, props):
        for sense in props['current_senses']:
            sense['metadata'] = {'id': str(uuid7()), 'tags': [], 'image_keywords': ['money building'], 'advanced': {}}
        return props['current_senses']

    def keep_all(self):
        return self.refine_with(lambda ids: [{'id': i, 'action': 'keep'} for i in ids])

    @override_settings(PIXABAY_API_KEY='')
    def test_no_pixabay_key_skips_images(self):
        with mock.patch('utils.celery.fetch_image.find_image') as find_image:
            result = self.run_pipeline(lambda m: stream_pieces(DRAFT), self.keep_all(), metadata=self.metadata_with_keywords)
        self.assertTrue(result['is_valid'])
        find_image.assert_not_called()
        self.assertEqual([s.image_preview for s in self.senses()], [None, None])

    @override_settings(PIXABAY_API_KEY='test-key')
    def test_images_are_saved_with_senses_when_key_present(self):
        context = ImageContext.objects.create(description='money building', provider='pixabay')
        socket = AsyncMock()
        with mock.patch('utils.celery.fetch_image.find_image', return_value=(context.id, '/upload/images/v1/p.webp')) as find_image, \
                mock.patch('utils.celery.fetch_image.socket_message', socket):
            result = self.run_pipeline(lambda m: stream_pieces(DRAFT), self.keep_all(), metadata=self.metadata_with_keywords)
        self.assertTrue(result['is_valid'])
        self.assertEqual(find_image.call_count, 2)  # mỗi nghĩa 1 lần
        for sense in self.senses():
            self.assertEqual((sense.image_preview, sense.image_context_id), ('/upload/images/v1/p.webp', context.id))
        self.assertEqual([c.args[1]['type'] for c in socket.call_args_list], ['PARTIAL_IMAGE', 'PARTIAL_IMAGE'])
        cached = self.cache.cache_word_get_data('en', self.WORD)
        self.assertEqual({s['image_preview'] for s in cached['senses'].values()}, {'/upload/images/v1/p.webp'})

    @override_settings(PIXABAY_API_KEY='test-key')
    def test_image_failure_does_not_block_saving(self):
        with mock.patch('utils.celery.fetch_image.find_image', side_effect=RuntimeError('pixabay down')):
            result = self.run_pipeline(lambda m: stream_pieces(DRAFT), self.keep_all(), metadata=self.metadata_with_keywords)
        self.assertTrue(result['is_valid'])
        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'COMPLETED')
        self.assertEqual([s.image_preview for s in self.senses()], [None, None])

    def test_worker_keeps_symbols_in_word(self):
        self.prepare('c++', 'en')
        prompts = []

        def script(model):
            return stream_pieces(DRAFT)

        with mock.patch('utils.ai.word_render.render_word_prompt', side_effect=lambda w, l: prompts.append(w) or w):
            self.run_pipeline(script, self.keep_all())
        self.assertEqual(prompts, ['c++'])  # không bị cắt thành 'c'
        self.assertEqual(self.cache.cache_word_get_data('en', 'c++')['status'], 'CACHED')  # đúng key view đang chờ

    @override_settings(AI_REFINE_ALLOW_ADD=True)
    def test_refine_can_add_a_missing_sense(self):
        def builder(ids):
            return [{'id': i, 'action': 'keep'} for i in ids] + [{
                'action': 'add', 'pos': 'verb', 'definition': 'To deposit money in a bank.', 'level': 'B1',
                'examples': ['I bank at the corner branch.'], 'reason': 'common verb sense is missing',
            }]

        result = self.run_pipeline(lambda m: stream_pieces(DRAFT), self.refine_with(builder))
        self.assertTrue(result['is_valid'])
        senses = self.senses()
        self.assertEqual(len(senses), 3)
        added = [s for s in senses if (s.ai_review or {}).get('added')]
        self.assertEqual(len(added), 1)
        new = added[0]
        self.assertEqual((new.pos, new.quality_status, new.ai_pass, new.ai_model), ('verb', 'refined', 2, 'fake-pro'))
        self.assertEqual(new.contents['definition']['vi'], {'value': 'vi: To deposit money in a bank.'})  # được dịch
        self.assertEqual(new.contents['translations'], {'vi': ['vi-word']})
        self.assertIsNotNone(new.metadata_id)  # và có metadata
        self.assertEqual(set(self.cache.cache_word_get_data('en', self.WORD)['senses']), {str(s.id) for s in senses})

    @override_settings(AI_BACKGROUND_TIMEOUT=1)
    def test_stuck_translation_does_not_block_saving(self):
        async def stuck_metadata(props):
            await asyncio.sleep(30)  # lệnh gọi Gemini treo

        started = time.monotonic()
        result = self.run_pipeline(lambda m: stream_pieces(DRAFT), self.refine_with(lambda ids: [{'id': i, 'action': 'keep'} for i in ids]), metadata=stuck_metadata)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(result['is_valid'])
        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'COMPLETED')
        self.assertEqual(len(self.senses()), 2)
        self.assertEqual({s.quality_status for s in self.senses()}, {'refined'})

    def test_cache_failure_does_not_lose_translation_or_refine(self):
        with mock.patch.object(WordCacheManager, 'cache_word_add_sense', side_effect=ConnectionError('redis down')):
            self.run_pipeline(lambda m: stream_pieces(DRAFT), self.refine_with(lambda ids: [{'id': ids[0], 'action': 'fix', 'level': 'B2', 'reason': 'x'}, {'id': ids[1], 'action': 'keep'}]))
        self.assertTrue(self.translate_calls)  # bản dịch vẫn được tạo dù cache hỏng
        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'COMPLETED')
        # Refine áp xong thì lưu đúng bản refined, không rơi về nửa draft nửa refined
        self.assertEqual({(s.quality_status, s.ai_pass) for s in self.senses()}, {('refined', 2)})
        self.assertIn('B2', {s.level for s in self.senses()})

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


class ClaudeSchemaTests(TestCase):
    """Schema kiểu Gemini đổi sang JSON Schema cho structured outputs của Claude (sai là API trả 400)."""

    def walk(self, node):
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from self.walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from self.walk(value)

    def test_every_schema_converts_cleanly(self):
        from utils.ai import claude
        from utils.ai.schema import word_schema
        for schema in (word_schema, complex_schema, refine.refine_schema):
            out = claude.output_config(schema)['format']['schema']
            for node in self.walk(out):
                self.assertNotIn('maxItems', node)
                if isinstance(node.get('type'), str):
                    self.assertEqual(node['type'], node['type'].lower())
                if node.get('type') == 'object':
                    self.assertIs(node['additionalProperties'], False)
        # metadata đứng trước senses: bộ đọc stream cần biết sớm từ có bị từ chối không
        self.assertEqual(list(claude.to_json_schema(word_schema)['properties'])[:3], ['word', 'metadata', 'senses'])
        self.assertIn('maxItems', word_schema['properties']['senses']['items']['properties']['examples'])  # schema gốc không bị sửa

    def test_effort_is_optional(self):
        from utils.ai import claude
        self.assertNotIn('effort', claude.output_config(refine.refine_schema))
        self.assertEqual(claude.output_config(refine.refine_schema, effort='low')['effort'], 'low')


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

    def _languages_for_new_word(self, user_languages, **props):
        """Chạy ai_create_translate_sema cho 1 nghĩa tiếng Pháp, trả về danh sách ngôn ngữ đích mà bước dịch nhận."""
        contents = {'definition': {'fr': {'value': 'def'}}, 'usage': {'fr': {'value': ''}}, 'examples': [{'fr': {'value': 'ex'}}]}
        sense = {'id': 's1', 'contents': contents}
        chunk = AsyncMock(return_value=None)
        with mock.patch('utils.ai.translate.process_translation_chunk', chunk):
            async_to_sync(ai_create_translate_sema)({
                'word_value': 'zzbase', 'language_code': 'fr', 'user_language_code': user_languages,
                'missing_translate': [sense], 'current_senses': [sense], **props,
            })
        return sorted(chunk.call_args.args[3])

    def test_new_word_translates_english_and_user_languages_only(self):
        self.assertEqual(self._languages_for_new_word(['vi', 'ja']), ['en', 'ja', 'vi'])

    def test_seed_job_can_ask_for_more_languages(self):
        self.assertEqual(self._languages_for_new_word(['vi'], base_languages=['en', 'zh', 'fr']), ['en', 'vi', 'zh'])  # tiếng Pháp là ngôn ngữ gốc nên không tự dịch sang chính nó

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

    def test_word_key_matches_worker_and_app_room(self):
        self.words.update({'ice cream', 'c++'})
        self.assertEqual(self.get('  Ice   Cream ').status_code, 201)
        self.assertEqual(self.get('C++').status_code, 201)
        self.assertEqual(sorted(AIWord.objects.values_list('value', flat=True)), ['c++', 'ice cream'])
        self.assertEqual(self.cache.cache_word_get_data('en', 'c++')['status'], 'PROCESSING')
        # Gõ khác khoảng trắng / hoa thường vẫn vào chờ chung, không tạo thêm từ
        self.assertEqual(self.get('ice cream').status_code, 202)
        self.assertEqual(AIWord.objects.count(), 2)

    def test_error_is_retried_after_cool_down_and_does_not_leak_cache(self):
        word = AIWord.objects.create(value='zzerr', language_code='en', status='FAILED')
        self.words.add('zzerr')
        self.cache.cache_word_init('en', 'zzerr', 'vi')
        self.cache.cache_word_set_status('en', 'zzerr', 'ERROR')
        res = self.get('zzerr')
        self.assertEqual(res.status_code, 500)
        self.assertEqual(set(res.data['data']), {'value', 'language_code', 'retry_after'})  # không trả nguyên cache

        self.cache.r.json().set('word:en:zzerr', '.finished_at', 0)  # quá thời gian chờ
        res = self.get('zzerr')
        self.assertEqual(res.status_code, 201)  # xuống DB (FAILED) và tạo lại trên cùng bản ghi
        self.assertEqual(self.queue.jobs[-1][1]['word_id'], str(word.id))

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


@override_settings(PIXABAY_API_KEY='test-key')
class FindImageTests(TestCase):
    HITS = {'hits': [
        {'id': 11, 'imageWidth': 640, 'imageHeight': 400, 'webformatURL': 'https://cdn/11.png', 'user': 'a', 'pageURL': 'https://p/11'},
        {'id': 12, 'imageWidth': 640, 'imageHeight': 400, 'webformatURL': 'https://cdn/12.png', 'user': 'b', 'pageURL': 'https://p/12'},
    ]}

    def fake_save(self, image, name, url):
        if name.endswith('12'):
            raise OSError('broken image')
        image.url = f'/upload/images/v1/{name}.webp'
        image.save()

    def search(self, payload):
        response = mock.Mock()
        response.json.return_value = payload
        return mock.patch('utils.celery.fetch_image.requests.get', return_value=response)

    def test_fetches_once_then_reuses_context(self):
        with self.search(self.HITS) as get, mock.patch('utils.celery.fetch_image.save_sense_image', self.fake_save):
            first = find_image(['  Money   Building ', 'bank'])
            second = find_image(['money building'])
        self.assertEqual(get.call_count, 1)  # lần 2 dùng lại ImageContext, không gọi Pixabay
        self.assertEqual(get.call_args.kwargs['params']['q'], 'money building')
        self.assertEqual(first, second)
        self.assertEqual(first[1], '/upload/images/v1/pixabay_11.webp')
        # Ảnh tải hỏng không để lại bản ghi rỗng
        self.assertEqual(list(ImageLibrary.objects.values_list('provider_id', flat=True)), ['11'])

    def test_tries_next_keyword_when_no_hits(self):
        with self.search({'hits': []}) as get:
            self.assertIsNone(find_image(['a', 'b', 'c', 'd']))
        self.assertEqual(get.call_count, 3)  # tối đa MAX_KEYWORDS
        self.assertFalse(ImageContext.objects.exists())

    def test_network_error_returns_none(self):
        with mock.patch('utils.celery.fetch_image.requests.get', side_effect=OSError('timeout')):
            self.assertIsNone(find_image(['bank']))

    @override_settings(PIXABAY_API_KEY='')
    def test_queue_job_is_skipped_without_key(self):
        with mock.patch('utils.celery.fetch_image.find_image') as find:
            self.assertIsNone(async_to_sync(get_image_by_keyword)({'sense_id': str(uuid7()), 'keywords': ['bank']}))
        find.assert_not_called()


class RegenerateTests(PipelineBase):
    """Admin tạo lại từ: nghĩa cũ dùng tới khi bản mới lưu xong rồi thôi chính thức; AI từ chối / lỗi thì giữ bản cũ."""

    def setUp(self):
        super().setUp()
        self.word.status = 'COMPLETED'
        self.word.save()
        old = make_sense(str(uuid7()), definition='Old wrong meaning.')
        old.update(word_id=self.word.id, word_value=self.WORD)
        self.old = AISense.objects.create(**old)
        self.admin = get_user_model().objects.create_user('regen-admin', is_staff=True)
        self.queue = FakeQueue()

    def regenerate(self, user=None):
        self.client = APIClient()
        self.client.force_authenticate(user or self.admin)
        with mock.patch('ai.generation.push_job', self.queue.push_job):
            return self.client.post(f'/api/ai-words/{self.word.id}/regenerate/', {}, format='json')

    def run_job(self, payload):
        job = self.queue.jobs[-1][1]
        patches = [
            mock.patch('utils.ai.claude.client', fake_claude(lambda m: stream_pieces(payload))),
            mock.patch('utils.ai.word_render.ai_create_translate_sema', self.fake_translate),
            mock.patch('utils.ai.word_render.ai_create_metadata', self.fake_metadata),
            mock.patch('utils.ai.word_render.request_refinement', AsyncMock(return_value=None)),
            mock.patch('utils.ai.word_render.socket_message', AsyncMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return async_to_sync(ai_create_new_word_sema)(job)

    def test_new_senses_replace_old_ones(self):
        res = self.regenerate()
        self.assertEqual((res.status_code, res.data['replacing']), (202, 1))
        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'PROCESSING')
        self.assertEqual(self.queue.jobs[-1][1]['replace_sense_ids'], [str(self.old.id)])

        self.run_job(DRAFT)
        self.word.refresh_from_db()
        self.old.refresh_from_db()
        self.assertEqual(self.word.status, 'COMPLETED')
        self.assertFalse(self.old.is_official)  # vẫn còn cho bộ từ đang trỏ tới, nhưng không còn trong get-word
        self.assertEqual(len(load_word(id=self.word.id).senses.all()), 2)

    def test_rejection_keeps_old_senses(self):
        self.regenerate()
        self.run_job({**DRAFT, "metadata": {"should_be_saved": False}})
        self.word.refresh_from_db()
        self.old.refresh_from_db()
        self.assertEqual((self.word.status, self.old.is_official), ('COMPLETED', True))
        self.assertIsNone(self.cache.cache_word_get_data('en', self.WORD))  # không để NOT_FOUND che bản cũ

    def test_only_admin_and_not_while_processing(self):
        self.assertEqual(self.regenerate(get_user_model().objects.create_user('plain')).status_code, 403)
        AIWord.objects.filter(pk=self.word.pk).update(status='PROCESSING', updated_at=timezone.now())
        self.assertEqual(self.regenerate().status_code, 409)


class WordSearchTests(APITestCase):
    def setUp(self):
        for value, state in (('zzapple', 'COMPLETED'), ('zzapplication', 'COMPLETED'), ('zzapp', 'COMPLETED'),
                             ('zzapply', 'PROCESSING'), ('zzbanana', 'COMPLETED')):
            AIWord.objects.create(value=value, language_code='en', status=state)
        AIWord.objects.create(value='zzapple', language_code='fr', status='COMPLETED')

    def search(self, **q):
        return self.client.get('/api/ai-words/search/', q)

    def test_prefix_then_fuzzy(self):
        self.assertEqual([w['value'] for w in self.search(q='ZZApp', lang='en').data], ['zzapp', 'zzapple', 'zzapplication'])
        self.assertEqual([w['value'] for w in self.search(q='zzaple', lang='en', limit=1).data], ['zzapple'])
        self.assertEqual(self.search(q='', lang='en').status_code, 400)

    @override_settings(DEBUG=False)
    def test_clear_is_debug_only(self):
        admin = get_user_model().objects.create_user('clear-admin', is_staff=True)
        self.client.force_authenticate(admin)
        self.assertEqual(self.client.delete('/api/ai-words/clear/').status_code, 403)
        self.assertEqual(AIWord.objects.count(), 6)


class BrokenStreamTests(PipelineBase):
    """Draft đứt giữa stream: nghĩa dở dang không được lưu như từ hoàn chỉnh."""

    ONE_SENSE = {**DRAFT, "senses": [{**DRAFT["senses"][0], "definition": {"value": "From the second model."}}]}

    def run_draft(self, script):
        socket = AsyncMock()
        patches = [
            mock.patch('utils.ai.claude.client', fake_claude(script)),
            mock.patch('utils.ai.word_render.ai_create_translate_sema', self.fake_translate),
            mock.patch('utils.ai.word_render.ai_create_metadata', self.fake_metadata),
            mock.patch('utils.ai.word_render.request_refinement', AsyncMock(return_value=None)),
            mock.patch('utils.ai.word_render.socket_message', socket),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return async_to_sync(ai_create_new_word_sema)(self.job), socket

    @staticmethod
    def broken(payload):
        """Trả nửa đầu stream (đủ metadata + nghĩa đầu) rồi đứt kết nối."""
        pieces = stream_pieces(payload)
        cut = len(json.dumps(payload)) * 2 // 3 // 23

        def gen():
            yield from pieces[:cut]
            raise ConnectionError('stream dropped')
        return gen()

    @override_settings(AI_DRAFT_MODELS=['m1', 'm2'])
    def test_falls_back_to_next_model_and_drops_partial_senses(self):
        result, socket = self.run_draft(lambda m: self.broken(DRAFT) if m == 'm1' else stream_pieces(self.ONE_SENSE))
        self.assertTrue(result['is_valid'])
        senses = self.senses()
        self.assertEqual([s.contents['definition']['en']['value'] for s in senses], ['From the second model.'])
        removed = [c.args[1]['payload'] for c in socket.call_args_list if c.args[1]['type'] == 'SENSE_REMOVED']
        self.assertEqual(len(removed), 1)
        self.assertNotIn(str(senses[0].id), removed[0])

    @override_settings(AI_DRAFT_MODELS=['m1'])
    def test_broken_stream_without_fallback_fails(self):
        result, _ = self.run_draft(lambda m: self.broken(DRAFT))
        self.assertFalse(result['is_valid'])
        self.word.refresh_from_db()
        self.assertEqual(self.word.status, 'FAILED')
        self.assertEqual(self.senses(), [])
