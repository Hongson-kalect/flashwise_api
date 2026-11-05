"""Điểm xếp hạng nghĩa (ai/scores.py), nghĩa chính (interact/mains.py), đề xuất của admin và thứ tự nghĩa ở trang từ (ai/queries.py)."""
from django.contrib.auth import get_user_model
from django.core.cache import cache as django_cache
from rest_framework.test import APITestCase

from ai.models import AISense, AIWord
from ai.queries import order_senses
from ai.scores import MAIN_WEIGHT, RECOMMEND_BONUS, refresh_sense_scores
from user.models import UserProfile
from utils.redis.word_init import WordCacheManager

User = get_user_model()


def sense_contents(text):
    return {'definition': {'en': {'value': text}, 'vi': {'value': text + ' vi'}}, 'translations': {'vi': [text]},
            'examples': [{'en': {'value': text + ' example'}, 'vi': {'value': text + ' ví dụ'}}]}


class ScoreTestBase(APITestCase):
    def setUp(self):
        django_cache.clear()
        self.cache = WordCacheManager()
        self.word = AIWord.objects.create(value='zzscoreword', language_code='en', status='COMPLETED')
        # Thứ tự id = thứ tự AI trả ra: verb, noun1, noun2
        self.verb, self.noun1, self.noun2 = [
            AISense.objects.create(word=self.word, word_value='zzscoreword', language_code='en', pos=pos, is_official=True,
                                   contents=sense_contents(pos + str(i)))
            for i, pos in enumerate(('verb', 'noun', 'noun'))
        ]
        self.user = self.account('alice')
        self.guest = self.account('visitor', tier='guest')
        self.admin = User.objects.create_user('root', is_staff=True)

    def tearDown(self):
        self.cache.cache_word_clear_specific('en', 'zzscoreword')

    def account(self, name, tier='normal_user'):
        user = User.objects.create_user(name)
        UserProfile.objects.update_or_create(user=user, defaults={'tier': tier})
        return user

    def score(self, sense):
        return AISense.objects.get(pk=sense.pk).score

    def main_count(self, sense):
        return AISense.objects.get(pk=sense.pk).main_count

    def set_main(self, user, sense, method='post'):
        self.client.force_authenticate(user)
        return getattr(self.client, method)(f'/api/ai-senses/{sense.id}/main/')

    def order(self, user=None):
        self.cache.cache_word_clear_specific('en', 'zzscoreword')
        self.client.force_authenticate(user)
        res = self.client.get('/api/ai-words/get-word/', {'value': 'zzscoreword', 'lang': 'en', 'user_lang': 'vi'})
        self.assertEqual(res.status_code, 200, res.data)
        return list(res.data['data']['senses'])


class ScoreFormulaTests(ScoreTestBase):
    def test_signals_and_their_relative_strength(self):
        AISense.objects.filter(pk=self.verb.pk).update(learner_count=10)
        AISense.objects.filter(pk=self.noun1.pk).update(main_count=3)
        AISense.objects.filter(pk=self.noun2.pk).update(like_count=500, views=500)  # like, view không tính
        refresh_sense_scores()
        learned, chosen, liked = self.score(self.verb), self.score(self.noun1), self.score(self.noun2)
        self.assertEqual(liked, 0)
        self.assertGreater(learned, 0)
        self.assertGreater(chosen, learned)  # đặt chính mạnh hơn lượt học
        AISense.objects.filter(pk=self.noun2.pk).update(recommended_at='2026-01-01T00:00:00Z')
        refresh_sense_scores(pk=self.noun2.pk)
        self.assertEqual(self.score(self.noun2), RECOMMEND_BONUS)
        AISense.objects.filter(pk=self.noun1.pk).update(main_count=1000)
        refresh_sense_scores(pk=self.noun1.pk)
        self.assertLess(self.score(self.noun1), RECOMMEND_BONUS)  # 1000 người đặt chính vẫn chưa bằng một đề xuất của admin
        self.assertGreater(MAIN_WEIGHT, 0)

    def test_first_review_raises_score_of_sense_and_its_variants(self):
        from tracking.stats import apply_review_stats
        from tracking.models import ReviewLog
        from uuid6 import uuid7
        from django.utils import timezone
        custom = AISense.objects.create(word=self.word, word_value='zzscoreword', language_code='en', original=self.noun1, is_official=False, contents={})
        row = ReviewLog.objects.create(id=uuid7(), user=self.user, original_sense_id=self.noun1.id, rating=3, is_fsrs=True, reviewed_at=timezone.now())
        apply_review_stats(self.user, [row])
        self.assertGreater(self.score(self.noun1), 0)
        self.assertEqual(self.score(custom), self.score(self.noun1))
        self.assertEqual(self.score(self.noun2), 0)


class MainSenseTests(ScoreTestBase):
    def test_set_main_counts_once_and_switching_revokes_old_vote(self):
        res = self.set_main(self.user, self.noun1)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['main'], res.data['main_count']), (True, 1))
        self.assertEqual(self.set_main(self.user, self.noun1).data['main_count'], 1)  # gọi lại không cộng thêm
        self.assertGreater(self.score(self.noun1), 0)

        self.set_main(self.user, self.noun2)  # đổi nghĩa chính: phiếu cũ thu hồi
        self.assertEqual((self.main_count(self.noun1), self.main_count(self.noun2)), (0, 1))
        self.assertEqual(self.score(self.noun1), 0)

        bob = self.account('bob')
        self.set_main(bob, self.noun2)
        self.assertEqual(self.main_count(self.noun2), 2)  # mỗi người một phiếu

        res = self.set_main(self.user, self.noun2, 'delete')
        self.assertEqual((res.data['main'], res.data['main_count']), (False, 1))
        self.assertEqual(self.set_main(self.user, self.noun2, 'delete').data['main_count'], 1)  # bỏ lại không trừ thêm

    def test_guest_main_is_personal_only(self):
        self.assertEqual(self.set_main(self.guest, self.noun1).data['main_count'], 0)
        self.assertEqual(self.score(self.noun1), 0)
        self.assertEqual(self.order(self.guest)[0], str(self.noun1.id))  # nhưng guest vẫn thấy nghĩa chính của mình đứng đầu

    def test_own_custom_sense_as_main_does_not_count(self):
        custom = AISense.objects.create(word=self.word, word_value='zzscoreword', language_code='en', pos='noun', is_official=False,
                                        created_by=self.user, contents=sense_contents('mine'))
        self.assertEqual(self.set_main(self.user, custom).data['main_count'], 0)

    def test_requires_login_and_visible_sense(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.post(f'/api/ai-senses/{self.noun1.id}/main/').status_code, (401, 403))
        others = AISense.objects.create(word=self.word, word_value='zzscoreword', language_code='en', pos='noun', is_official=False,
                                        created_by=self.account('carol'), contents={})
        self.assertEqual(self.set_main(self.user, others).status_code, 404)  # nghĩa riêng của người khác không thấy được


class RecommendTests(ScoreTestBase):
    def test_admin_recommends_and_removes(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post(f'/api/ai-senses/{self.noun2.id}/recommend/').status_code, 403)  # không phải admin
        self.client.force_authenticate(self.admin)
        res = self.client.post(f'/api/ai-senses/{self.noun2.id}/recommend/')
        self.assertEqual((res.status_code, res.data['recommended'], res.data['score']), (200, True, RECOMMEND_BONUS))
        self.assertEqual(AISense.objects.get(pk=self.noun2.pk).recommended_by_id, self.admin.id)
        res = self.client.delete(f'/api/ai-senses/{self.noun2.id}/recommend/')
        self.assertEqual((res.data['recommended'], res.data['score']), (False, 0))
        custom = AISense.objects.create(word=self.word, word_value='zzscoreword', language_code='en', is_official=False, contents={})
        self.assertEqual(self.client.post(f'/api/ai-senses/{custom.id}/recommend/').status_code, 400)  # chỉ nghĩa chính thức


class PayloadFlagsTests(ScoreTestBase):
    def senses(self, user=None):
        self.cache.cache_word_clear_specific('en', 'zzscoreword')
        self.client.force_authenticate(user)
        return self.client.get('/api/ai-words/get-word/', {'value': 'zzscoreword', 'lang': 'en', 'user_lang': 'vi'}).data['data']['senses']

    def test_recommended_flag_is_public_and_is_main_is_personal(self):
        self.client.force_authenticate(self.admin)
        self.client.post(f'/api/ai-senses/{self.noun1.id}/recommend/')
        self.set_main(self.user, self.noun2)
        mine = self.senses(self.user)
        self.assertEqual([(s['recommended'], s.get('is_main', False)) for s in mine.values()], [(False, True), (True, False), (False, False)])
        others = self.senses(self.account('erin'))
        self.assertTrue(others[str(self.noun1.id)]['recommended'])
        self.assertFalse(any(s.get('is_main') for s in others.values()))
        self.assertFalse(any(s.get('is_main') for s in self.senses(None).values()))


class CacheClearTests(ScoreTestBase):
    def test_clear_all_only_removes_word_cache(self):
        r = self.cache.r
        r.set('zz_other_key', '1')
        self.addCleanup(r.delete, 'zz_other_key')
        self.order()  # đổ cache của từ
        self.assertTrue(r.exists('word:en:zzscoreword'))
        self.assertGreaterEqual(self.cache.cache_word_clear_all(), 1)
        self.assertFalse(r.exists('word:en:zzscoreword'))
        self.assertTrue(r.exists('zz_other_key'))  # khoá không phải cache từ còn nguyên


class SenseOrderTests(ScoreTestBase):
    def test_pos_ordered_by_best_sense_then_sense_by_score(self):
        self.assertEqual(self.order(), [str(self.verb.id), str(self.noun1.id), str(self.noun2.id)])  # chưa ai chấm: thứ tự AI
        self.client.force_authenticate(self.admin)
        self.client.post(f'/api/ai-senses/{self.noun2.id}/recommend/')
        # noun2 điểm cao nhất nên loại từ noun lên đầu, trong noun thì noun2 trước
        self.assertEqual(self.order(), [str(self.noun2.id), str(self.noun1.id), str(self.verb.id)])

    def test_users_main_then_own_edit_then_score(self):
        self.client.force_authenticate(self.admin)
        self.client.post(f'/api/ai-senses/{self.noun2.id}/recommend/')
        custom = AISense.objects.create(word=self.word, word_value='zzscoreword', language_code='en', pos='verb', is_official=False,
                                        created_by=self.user, original=self.verb, contents=sense_contents('mine'))
        self.set_main(self.user, self.noun1)
        order = self.order(self.user)
        # nghĩa chính của mình, rồi bản sửa của mình (thay nghĩa verb gốc), rồi theo điểm chung
        self.assertEqual(order, [str(self.noun1.id), str(custom.id), str(self.noun2.id)])
        self.assertEqual(self.order(self.account('dave'))[0], str(self.noun2.id))  # người khác thấy thứ tự chung

    def test_main_on_replaced_original_points_to_the_custom_copy(self):
        custom = AISense.objects.create(word=self.word, word_value='zzscoreword', language_code='en', pos='verb', is_official=False,
                                        created_by=self.user, original=self.verb, contents=sense_contents('mine'))
        self.set_main(self.user, self.verb)
        self.assertEqual(self.order(self.user)[0], str(custom.id))

    def test_order_senses_unit(self):
        senses = {'a': {'pos': 'verb', 'score': 0}, 'b': {'pos': 'noun', 'score': 5}, 'c': {'pos': 'verb', 'score': 5}, 'd': {'pos': 'noun'}}
        self.assertEqual(list(order_senses(senses)), ['c', 'a', 'b', 'd'])  # verb và noun cùng điểm cao nhất: loại từ xuất hiện trước (verb) đứng trước; trong verb c trước a
        self.assertEqual(list(order_senses(senses, ['a', 'zzz'])), ['a', 'b', 'd', 'c'])  # a ghim đầu, id lạ bị bỏ qua; còn lại noun (b, d) trước verb (c) vì b xuất hiện trước c
        self.assertEqual(list(order_senses({})), [])
