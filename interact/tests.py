from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from ai.models import AISense, AIWord
from core.models import Collection
from interact.models import ModerationAction, Report
from user.models import ReputationEvent, UserProfile
from user.reputation import record, report_trust

User = get_user_model()
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM_CACHE)
class ModerationTestBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user('alice', password='x')
        self.bob = User.objects.create_user('bob', password='x')
        self.admin = User.objects.create_user('admin', password='x', is_staff=True)
        for user in (self.alice, self.bob, self.admin):
            UserProfile.objects.get_or_create(user=user)
        self.word = AIWord.objects.create(value='bank', language_code='en', status='COMPLETED')
        self.sense = AISense.objects.create(
            word=self.word, word_value='bank', language_code='en', is_official=True,
            contents={'definition': {'en': 'old', 'vi': 'cũ'}, 'examples': [{'en': 'A bank.'}]},
        )
        self.client.force_authenticate(self.alice)

    def profile(self, user):
        return UserProfile.objects.get(user=user)

    def report(self, target_type, target_id, reason='Sai', user=None):
        if user is not None:
            self.client.force_authenticate(user)
        return self.client.post('/api/reports/', {'target_type': target_type, 'target_id': str(target_id), 'reason': reason}, format='json')

    def as_admin(self):
        self.client.force_authenticate(self.admin)


class ReportTests(ModerationTestBase):
    def test_report_dedupes_and_checks_target(self):
        res = self.report('sense', self.sense.id)
        self.assertEqual(res.status_code, 201)
        res2 = self.report('sense', self.sense.id, reason='Sai hẳn')
        self.assertEqual((res2.status_code, res2.data['id']), (200, res.data['id']))  # gửi lại chỉ cập nhật
        self.assertEqual(Report.objects.get().reason, 'Sai hẳn')
        self.assertEqual(Report.objects.get().weight, 0.5)

        private = AISense.objects.create(word=self.word, word_value='bank', language_code='en', is_official=False, created_by=self.bob, contents={})
        self.assertEqual(self.report('sense', private.id).status_code, 404)  # không thấy thì không report được
        self.assertEqual(self.report('word', self.sense.id).status_code, 404)
        self.assertEqual(self.report('user', self.profile(self.bob).id).status_code, 201)

    def test_queue_orders_by_trust_and_resolve_updates_reputation(self):
        collection = Collection.objects.create(name='Bad', language_code='en', released=True, created_by=self.bob)
        # alice đã có 3 report đúng nên báo nặng ký hơn
        for _ in range(3):
            record(self.alice, 'report_accepted')
        self.report('collection', collection.id)
        self.report('sense', self.sense.id, user=self.bob)

        self.assertEqual(self.client.get('/api/reports/queue/').status_code, 403)
        self.as_admin()
        targets = self.client.get('/api/reports/queue/').data['targets']
        self.assertEqual([t['target_type'] for t in targets], ['collection', 'sense'])
        self.assertAlmostEqual(targets[0]['weight'], report_trust(3, 0), places=3)
        self.assertEqual(targets[0]['target']['name'], 'Bad')

        res = self.client.post('/api/reports/resolve/', {'target_type': 'sense', 'target_id': str(self.sense.id), 'outcome': 'spam', 'result': 'Nghĩa đúng'}, format='json')
        self.assertEqual(res.data['resolved'], 1)
        bob = self.profile(self.bob)
        self.assertEqual((bob.reputation, bob.report_rejected), (-5, 2))
        self.assertEqual(Report.objects.get(target_id=self.sense.id).status, 'ignored')

        res = self.client.post('/api/reports/resolve/', {
            'target_type': 'collection', 'target_id': str(collection.id), 'outcome': 'accepted', 'ban': {'reason': 'Xúc phạm'},
        }, format='json')
        self.assertEqual(res.data['ban']['action'], 'ban')
        alice = self.profile(self.alice)
        self.assertEqual((alice.reputation, alice.report_accepted), (8, 4))
        self.assertEqual(self.profile(self.bob).reputation, -15)  # tác giả bộ bị ban
        self.assertFalse(Collection.objects.get(pk=collection.pk).is_active)
        self.assertEqual(self.client.post('/api/reports/resolve/', {'target_type': 'collection', 'target_id': str(collection.id), 'outcome': 'accepted'}, format='json').status_code, 404)
        self.assertEqual(self.client.post('/api/reports/resolve/', {'target_type': 'collection', 'target_id': str(collection.id), 'outcome': 'x'}, format='json').status_code, 400)

        # Trọng số report mới theo độ tin cậy hiện tại
        self.report('word', self.word.id, user=self.bob)
        self.assertAlmostEqual(Report.objects.get(target_id=self.word.id).weight, report_trust(0, 2), places=3)


class BanTests(ModerationTestBase):
    def ban(self, target_type, target_id, **extra):
        self.as_admin()
        return self.client.post('/api/moderation/ban/', {'target_type': target_type, 'target_id': str(target_id), 'reason': 'x', **extra}, format='json')

    def unban(self, target_type, target_id):
        self.as_admin()
        return self.client.post('/api/moderation/unban/', {'target_type': target_type, 'target_id': str(target_id)}, format='json')

    def test_user_ban_blocks_public_actions_only_and_unban_restores(self):
        collection = Collection.objects.create(name='Bob', language_code='en', released=True, created_by=self.bob)
        frozen = AISense.objects.create(word=self.word, word_value='bank', language_code='en', is_official=False, is_frozen=True, created_by=self.bob, contents={})
        bob_profile = self.profile(self.bob)
        res = self.ban('user', bob_profile.id, days=3, hide_content=True)
        self.assertEqual(res.status_code, 201)
        self.assertFalse(Collection.objects.get(pk=collection.pk).is_active)
        self.assertFalse(AISense.objects.get(pk=frozen.pk).is_active)
        self.assertEqual(self.profile(self.bob).reputation, -50)

        self.client.force_authenticate(self.bob)
        res = self.client.post(f'/api/ai-senses/{self.sense.id}/like/')
        self.assertEqual((res.status_code, res.data['errorCode']), (403, 'user_banned'))
        self.assertEqual(self.report('sense', self.sense.id).status_code, 403)
        self.assertEqual(self.client.get('/api/me/').status_code, 200)  # vẫn dùng app bình thường
        self.assertEqual(self.client.post('/api/feedback/', {'message': 'Xin gỡ ban'}, format='json').status_code, 201)

        # Hết hạn thì tự mở
        UserProfile.objects.filter(pk=bob_profile.pk).update(ban_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.post(f'/api/ai-senses/{self.sense.id}/like/').status_code, 200)

        self.assertEqual(self.unban('user', bob_profile.id).status_code, 201)
        self.assertTrue(Collection.objects.get(pk=collection.pk).is_active)
        self.assertTrue(AISense.objects.get(pk=frozen.pk).is_active)
        self.assertTrue(self.profile(self.bob).is_active)
        self.assertEqual(self.profile(self.bob).reputation, 0)
        self.assertEqual(self.unban('user', bob_profile.id).status_code, 400)  # không còn bị ban

    def test_cannot_ban_staff_and_requires_admin(self):
        self.assertEqual(self.client.post('/api/moderation/ban/', {'target_type': 'word', 'target_id': str(self.word.id)}, format='json').status_code, 403)
        self.assertEqual(self.ban('user', self.profile(self.admin).id).status_code, 400)
        self.assertEqual(self.ban('word', self.sense.id).status_code, 404)

    def test_word_ban(self):
        self.assertEqual(self.ban('word', self.word.id).status_code, 201)
        self.client.force_authenticate(self.alice)
        res = self.client.get('/api/ai-words/get-word/', {'value': 'bank', 'lang': 'en', 'user_lang': 'vi'})
        self.assertEqual(res.status_code, 404)
        self.as_admin()
        self.assertEqual(self.client.post(f'/api/ai-words/{self.word.id}/regenerate/', {}, format='json').status_code, 409)
        self.assertEqual(self.unban('word', self.word.id).status_code, 201)
        self.assertEqual(AIWord.objects.get(pk=self.word.pk).status, 'COMPLETED')
        history = self.client.get('/api/moderation/', {'target_id': str(self.word.id)}).data['actions']
        self.assertEqual([a['action'] for a in history], ['unban', 'ban'])

    def test_collection_and_sense_ban_hide_from_everyone(self):
        collection = Collection.objects.create(name='Bob', language_code='en', released=True, created_by=self.bob)
        self.ban('collection', collection.id)
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.get(f'/api/collections/{collection.id}/').status_code, 404)  # cả tác giả
        self.assertEqual(self.profile(self.bob).reputation, -10)
        self.unban('collection', collection.id)
        self.assertEqual(self.profile(self.bob).reputation, 0)
        self.assertTrue(Collection.objects.get(pk=collection.pk).is_active)

        self.ban('sense', self.sense.id)
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.get(f'/api/ai-senses/{self.sense.id}/').status_code, 404)
        self.assertFalse(ReputationEvent.objects.filter(target_type='sense').exists())  # nghĩa chính thức: không trừ ai
        self.unban('sense', self.sense.id)
        self.assertEqual(self.client.get(f'/api/ai-senses/{self.sense.id}/').status_code, 200)
        self.assertEqual(ModerationAction.objects.count(), 4)


class PromoteAndScoreTests(ModerationTestBase):
    def test_promote_user_sense(self):
        custom = AISense.objects.create(
            word=self.word, word_value='bank', language_code='en', is_official=False, created_by=self.bob, contents={},
            delta={'definition': {'en': 'new'}}, original=self.sense,
        )
        self.assertEqual(self.client.post(f'/api/ai-senses/{custom.id}/promote/').status_code, 403)
        self.as_admin()
        res = self.client.post(f'/api/ai-senses/{custom.id}/promote/')
        self.assertEqual(res.data['replaced'], str(self.sense.id))
        custom.refresh_from_db()
        self.assertTrue(custom.is_official)
        self.assertEqual(custom.contents, {'definition': {'en': 'new'}, 'examples': [{'en': 'A bank.'}]})  # bản dịch cũ của định nghĩa bị bỏ
        self.assertEqual(custom.original_id, self.sense.id)  # tiến độ học theo original vẫn giữ
        self.assertFalse(AISense.objects.get(pk=self.sense.pk).is_official)
        self.assertEqual(self.profile(self.bob).reputation, 20)
        self.assertEqual(self.client.post(f'/api/ai-senses/{custom.id}/promote/').status_code, 400)

    def test_collection_score_prefers_quality(self):
        good = Collection.objects.create(name='Good', language_code='en', released=True, created_by=self.alice)
        trusted = Collection.objects.create(name='Trusted', language_code='en', released=True, created_by=self.bob)
        record(self.bob, 'manual', points=1000)  # uy tín rất cao nhưng bộ chưa ai thích
        self.client.post(f'/api/collections/{good.id}/like/')
        self.assertEqual(Collection.objects.get(pk=good.pk).score, 69)  # 100 * ln 2
        self.assertEqual(Collection.objects.get(pk=trusted.pk).score, 10)  # uy tín chỉ cộng tối đa 10
        names = [c['name'] for c in self.client.get('/api/collections/', {'lang': 'en'}).data['results']]
        self.assertEqual(names[:2], ['Good', 'Trusted'])
