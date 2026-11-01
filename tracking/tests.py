import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from uuid6 import uuid7

from ai.models import AISense, AIWord
from core.models import Collection
from interact.models import Feedback, LearnSession, Report, UserNote
from tracking.models import DayLearnLog, ReviewLog, UserSenseProgress
from user.models import UserProfile, UserSetting

User = get_user_model()
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def iso(delta_minutes=0):
    return (timezone.now() + timedelta(minutes=delta_minutes)).isoformat()


@override_settings(CACHES=LOCMEM_CACHE)
class SyncTestBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('alice', password='x')
        self.other = User.objects.create_user('bob', password='x')
        UserProfile.objects.get_or_create(user=self.user)
        UserProfile.objects.get_or_create(user=self.other)
        self.client.force_authenticate(self.user)
        word = AIWord.objects.create(value='bank', language_code='en', status='COMPLETED')
        self.sense = AISense.objects.create(word=word, word_value='bank', language_code='en', is_official=True, contents={})

    def push(self, **changes):
        return self.client.post('/api/sync/push/', {'changes': changes}, format='json')


class PushTests(SyncTestBase):
    def test_requires_login_and_valid_body(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.push().status_code, 401)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post('/api/sync/push/', {}, format='json').status_code, 400)
        self.assertEqual(self.push(unknown_table=[]).status_code, 400)
        self.assertEqual(self.push(review_log={}).status_code, 400)
        self.assertEqual(self.push(review_log=[{}] * 501).status_code, 400)

    def test_progress_create_update_and_lww(self):
        sid = str(uuid7())
        row = {'original_sense_id': sid, 'state': 2, 'stability': 5.5, 'difficulty': 4.2, 'repetitions': 3,
               'next_review_at': iso(1000), 'is_hidden': 0, 'updated_at': iso(-10)}
        res = self.push(user_sense_progress=[row])
        self.assertEqual(res.data['results']['user_sense_progress']['accepted'], 1)
        saved = UserSenseProgress.objects.get(user=self.user, original_sense_id=sid)
        self.assertEqual((saved.state, saved.stability), (2, 5.5))

        # Bản sửa cũ hơn bị bỏ qua, bản mới hơn được nhận
        res = self.push(user_sense_progress=[dict(row, state=1, updated_at=iso(-60))])
        self.assertEqual(res.data['results']['user_sense_progress']['skipped'], 1)
        saved.refresh_from_db()
        self.assertEqual(saved.state, 2)

        self.push(user_sense_progress=[dict(row, state=3, updated_at=iso(-1))])
        saved.refresh_from_db()
        self.assertEqual(saved.state, 3)
        self.assertEqual(UserSenseProgress.objects.filter(user=self.user).count(), 1)

    def test_progress_older_edit_with_later_review_still_wins(self):
        sid = str(uuid7())
        self.push(user_sense_progress=[{'original_sense_id': sid, 'state': 1, 'last_reviewed_at': iso(-100), 'updated_at': iso(-5)}])
        # Máy khác sửa sớm hơn nhưng có lần ôn mới hơn: lần ôn mới nhất không được mất
        self.push(user_sense_progress=[{'original_sense_id': sid, 'state': 2, 'last_reviewed_at': iso(-50), 'updated_at': iso(-20)}])
        self.assertEqual(UserSenseProgress.objects.get(original_sense_id=sid).state, 2)

    def test_progress_validation_and_isolation(self):
        sid = str(uuid7())
        res = self.push(user_sense_progress=[
            {'original_sense_id': 'nope'},
            {'original_sense_id': sid, 'state': 9},
            {'original_sense_id': sid, 'difficulty': 99},
            {'original_sense_id': sid, 'state': 1},
        ])
        out = res.data['results']['user_sense_progress']
        self.assertEqual(out['accepted'], 1)
        self.assertEqual(len(out['rejected']), 3)
        # Tiến độ của người khác không bị đụng tới
        UserSenseProgress.objects.create(user=self.other, original_sense_id=sid, state=3)
        self.push(user_sense_progress=[{'original_sense_id': sid, 'state': 0, 'updated_at': iso(10)}])
        self.assertEqual(UserSenseProgress.objects.get(user=self.other).state, 3)

    def test_progress_client_cannot_steal_other_users_row_id(self):
        theirs = UserSenseProgress.objects.create(user=self.other, original_sense_id=str(uuid7()), state=3)
        self.push(user_sense_progress=[{'id': str(theirs.id), 'original_sense_id': str(uuid7()), 'state': 1}])
        theirs.refresh_from_db()
        self.assertEqual((theirs.user, theirs.state), (self.other, 3))
        self.assertEqual(UserSenseProgress.objects.get(user=self.user).state, 1)

    def test_review_log_append_only_and_idempotent(self):
        sid, rid = str(uuid7()), str(uuid7())
        log = {'id': rid, 'original_sense_id': sid, 'rating': 3, 'duration_ms': 1200, 'state': 1, 'reviewed_at': iso(-3)}
        self.assertEqual(self.push(review_log=[log]).data['results']['review_log']['accepted'], 1)
        self.push(review_log=[dict(log, rating=1)])  # gửi lại: không sửa, không nhân đôi
        self.assertEqual(ReviewLog.objects.filter(user=self.user).count(), 1)
        self.assertEqual(ReviewLog.objects.get(pk=rid).rating, 3)
        bad = self.push(review_log=[dict(log, id=str(uuid7()), rating=7), {'original_sense_id': sid, 'rating': 3}])
        self.assertEqual(len(bad.data['results']['review_log']['rejected']), 2)

    def test_day_log_merges_by_max_and_updates_stats(self):
        today = timezone.now().date()
        self.push(day_learn_log=[{'date': str(today), 'words_learned': 5, 'xp_earned': 50, 'learn_time': 300}])
        self.push(day_learn_log=[{'date': str(today), 'words_learned': 3, 'xp_earned': 80}])  # máy kia ít hơn ở words_learned
        log = DayLearnLog.objects.get(user=self.user, date=today)
        self.assertEqual((log.words_learned, log.xp_earned, log.learn_time), (5, 80, 300))
        self.assertEqual(DayLearnLog.objects.filter(user=self.user).count(), 1)
        self.assertEqual(UserProfile.objects.get(user=self.user).total_xp, 80)

        res = self.push(day_learn_log=[{'date': str(today + timedelta(days=30))}, {'date': 'x'}, {'date': str(today), 'learn_time': 999999}])
        self.assertEqual(len(res.data['results']['day_learn_log']['rejected']), 3)

    def test_streak_counts_consecutive_days_and_keeps_yesterday(self):
        profile = UserProfile.objects.get(user=self.user)
        today = timezone.now().astimezone(ZoneInfo(profile.time_zone)).date()  # streak tính theo múi giờ của user
        days = [today - timedelta(days=n) for n in (1, 2, 3, 10, 11, 12, 13, 14)]
        self.push(day_learn_log=[{'date': str(d), 'words_learned': 1} for d in days])
        profile.refresh_from_db()
        self.assertEqual(profile.current_streak, 3)  # hôm nay chưa học vẫn giữ chuỗi tới hôm qua
        self.assertEqual(profile.max_streak, 5)

    def test_learn_session(self):
        sid, s1 = str(uuid7()), str(uuid7())
        item = {'id': sid, 'type': 'review', 'start_at': iso(-30), 'end_at': iso(-20), 'time': 600, 'senses': [s1]}
        self.assertEqual(self.push(learn_session=[item]).data['results']['learn_session']['accepted'], 1)
        session = LearnSession.objects.get(pk=sid)
        self.assertEqual((session.user, session.sense_count, session.senses), (self.user, 1, [s1]))

        self.push(learn_session=[dict(item, is_deleted=1)])
        self.assertTrue(LearnSession.objects.get(pk=sid).is_deleted)

        theirs = LearnSession.objects.create(user=self.other, start_at=timezone.now())
        res = self.push(learn_session=[
            dict(item, id=str(theirs.id)), dict(item, id=str(uuid7()), type='nope'), dict(item, id=str(uuid7()), senses=['x']), {'id': str(uuid7())},
        ])
        self.assertEqual(len(res.data['results']['learn_session']['rejected']), 4)
        theirs.refresh_from_db()
        self.assertEqual(theirs.user, self.other)

    def test_notes_create_update_delete_and_validation(self):
        note = {'sense_id': str(self.sense.id), 'content': ' hay ', 'updated_at': iso(-10)}
        self.assertEqual(self.push(user_note=[note]).data['results']['user_note']['accepted'], 1)
        self.assertEqual(UserNote.objects.get(user=self.user, sense=self.sense).content, 'hay')

        self.push(user_note=[dict(note, content='mới', updated_at=iso(-1))])
        self.assertEqual(UserNote.objects.filter(user=self.user, is_deleted=False).get().content, 'mới')
        stale = self.push(user_note=[dict(note, content='cũ', updated_at=iso(-500))])
        self.assertEqual(stale.data['results']['user_note']['skipped'], 1)

        self.push(user_note=[dict(note, is_deleted=1, updated_at=iso(0))])
        self.assertFalse(UserNote.objects.filter(user=self.user, is_deleted=False).exists())
        # Tạo lại sau khi xoá không vướng ràng buộc unique
        self.push(user_note=[dict(note, content='lại', updated_at=iso(5))])
        self.assertEqual(UserNote.objects.filter(user=self.user, is_deleted=False).count(), 1)

        res = self.push(user_note=[{'sense_id': str(uuid7()), 'content': 'x'}, {'sense_id': str(self.sense.id), 'content': '  '}])
        self.assertEqual(len(res.data['results']['user_note']['rejected']), 2)

    def test_settings(self):
        res = self.push(user_settings=[
            {'key': 'theme', 'value': 'dark', 'updated_at': iso(-5)},
            {'key': 'daily_goal', 'value': 30},
            {'key': 'bad key!', 'value': 1},
            {'key': 'big', 'value': 'x' * 5000},
        ])
        out = res.data['results']['user_settings']
        self.assertEqual((out['accepted'], len(out['rejected'])), (2, 2))
        self.push(user_settings=[{'key': 'theme', 'value': 'light', 'updated_at': iso(-100)}])
        self.assertEqual(UserSetting.objects.get(user=self.user, key='theme').value, 'dark')

    def test_profile_only_writable_fields(self):
        res = self.push(user_profile={'native_language': 'vi', 'learning_languages': ['en', 'ja'], 'time_zone': 'Asia/Tokyo',
                                      'total_xp': 99999, 'tier': 'VIP', 'is_active': False, 'fav_time': '20:30'})
        self.assertEqual(res.data['results']['user_profile']['accepted'], 1)
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual((profile.native_language, profile.learning_languages, profile.time_zone), ('vi', ['en', 'ja'], 'Asia/Tokyo'))
        self.assertEqual((profile.total_xp, profile.tier, profile.is_active), (0, 'guest', True))
        bad = self.push(user_profile={'time_zone': 'Mars/Base', 'learning_languages': ['e;']})
        self.assertEqual(len(bad.data['results']['user_profile']['rejected']), 2)


class PullTests(SyncTestBase):
    def pull(self, **params):
        if 'cursors' in params:
            params['cursors'] = json.dumps(params['cursors'])
        return self.client.get('/api/sync/pull/', params)

    def test_pull_returns_only_own_rows_and_tombstones(self):
        UserSenseProgress.objects.create(user=self.user, original_sense_id=str(uuid7()), state=2)
        UserSenseProgress.objects.create(user=self.user, original_sense_id=str(uuid7()), is_deleted=True)
        UserSenseProgress.objects.create(user=self.other, original_sense_id=str(uuid7()))
        res = self.pull(tables='user_sense_progress')
        self.assertEqual(res.status_code, 200)
        items = res.data['tables']['user_sense_progress']['items']
        self.assertEqual(len(items), 2)
        self.assertEqual({i['is_deleted'] for i in items}, {True, False})
        self.assertIn('user_profile', res.data)
        self.assertNotIn('review_log', self.pull().data['tables'])  # mặc định không kéo review_log

    def test_pagination_with_same_timestamp(self):
        for _ in range(5):
            UserSenseProgress.objects.create(user=self.user, original_sense_id=str(uuid7()))
        UserSenseProgress.objects.filter(user=self.user).update(updated_at=timezone.now())  # cả loạt cùng mốc thời gian

        seen, cursor = [], None
        for _ in range(10):
            params = {'tables': 'user_sense_progress', 'limit': 2}
            if cursor:
                params['cursors'] = {'user_sense_progress': cursor}
            page = self.pull(**params).data['tables']['user_sense_progress']
            seen += [i['id'] for i in page['items']]
            cursor = page['cursor']
            if not page['has_more']:
                break
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

        # Không còn gì mới sau con trỏ
        page = self.pull(tables='user_sense_progress', cursors={'user_sense_progress': cursor}).data['tables']['user_sense_progress']
        self.assertEqual((page['items'], page['has_more']), ([], False))

    def test_pull_after_change_only_returns_changed(self):
        UserSenseProgress.objects.create(user=self.user, original_sense_id=str(uuid7()))
        cursor = self.pull(tables='user_sense_progress').data['tables']['user_sense_progress']['cursor']
        second = UserSenseProgress.objects.create(user=self.user, original_sense_id=str(uuid7()))
        page = self.pull(tables='user_sense_progress', cursors={'user_sense_progress': cursor}).data['tables']['user_sense_progress']
        self.assertEqual([i['id'] for i in page['items']], [second.id])

    def test_all_tables_serialize(self):
        sid = str(uuid7())
        self.push(
            user_sense_progress=[{'original_sense_id': sid}],
            review_log=[{'id': str(uuid7()), 'original_sense_id': sid, 'rating': 4}],
            day_learn_log=[{'date': str(timezone.now().date()), 'words_learned': 1}],
            learn_session=[{'id': str(uuid7()), 'start_at': iso(-5), 'senses': [sid]}],
            user_note=[{'sense_id': str(self.sense.id), 'content': 'n'}],
            user_settings=[{'key': 'theme', 'value': {'a': [1]}}],
        )
        res = self.pull(tables='user_sense_progress,review_log,day_learn_log,learn_session,user_note,user_settings')
        self.assertEqual(res.status_code, 200)
        for name, table in res.data['tables'].items():
            self.assertEqual(len(table['items']), 1, name)

    def test_bad_params(self):
        self.assertEqual(self.pull(tables='nope').status_code, 400)
        self.assertEqual(self.pull(limit='x').status_code, 400)
        self.assertEqual(self.client.get('/api/sync/pull/', {'cursors': 'not json'}).status_code, 400)
        self.assertEqual(self.pull(cursors={'user_note': 'garbage'}).status_code, 400)
        self.assertEqual(self.pull(tables='user_note', limit=0).status_code, 200)  # limit kẹp về ít nhất 1


class MeTests(SyncTestBase):
    def test_me_get_and_patch(self):
        self.assertEqual(self.client.get('/api/me/').data['tier'], 'guest')
        res = self.client.patch('/api/me/', {'full_name': 'An', 'native_language': 'vi', 'total_xp': 5, 'tier': 'VIP', 'is_active': False}, format='json')
        self.assertEqual(res.status_code, 200)
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual((profile.full_name, profile.total_xp, profile.tier, profile.is_active), ('An', 0, 'guest', True))
        self.assertEqual(self.client.patch('/api/me/', {'gender': 'x'}, format='json').status_code, 400)

    def test_me_requires_login_and_app_config_is_public(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/me/').status_code, 401)
        res = self.client.get('/api/app-config/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('min_version', res.data)

    def test_profile_serializer_does_not_let_banned_user_unban(self):
        from user.serializers.Profile import ProfileSerializer
        profile = UserProfile.objects.get(user=self.user)
        UserProfile.objects.filter(pk=profile.pk).update(is_active=False)
        profile.refresh_from_db()
        serializer = ProfileSerializer(profile, data={'is_active': True}, partial=True)
        self.assertTrue(serializer.is_valid())
        serializer.save()
        self.assertFalse(UserProfile.objects.get(pk=profile.pk).is_active)


class LikeAndFeedbackTests(SyncTestBase):
    def test_like_toggle_is_idempotent(self):
        collection = Collection.objects.create(name='IELTS', language_code='en', is_official=True)
        url = f'/api/collections/{collection.id}/like/'
        self.assertEqual(self.client.post(url).data, {'liked': True, 'like_count': 1})
        self.assertEqual(self.client.post(url).data['like_count'], 1)  # bấm lại không cộng thêm
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(url).data['like_count'], 2)
        self.assertEqual(self.client.delete(url).data, {'liked': False, 'like_count': 1})
        self.assertEqual(self.client.delete(url).data['like_count'], 1)  # bỏ thích lại không trừ thêm

    def test_like_requires_login_and_visibility(self):
        private = Collection.objects.create(name='Riêng', language_code='en', created_by=self.other)
        self.assertEqual(self.client.post(f'/api/collections/{private.id}/like/').status_code, 404)
        self.client.force_authenticate(None)
        public = Collection.objects.create(name='Chung', language_code='en', is_official=True)
        self.assertEqual(self.client.post(f'/api/collections/{public.id}/like/').status_code, 401)

    def test_report_and_feedback(self):
        target = str(uuid7())
        res = self.client.post('/api/reports/', {'target_type': 'word', 'target_id': target, 'reason': 'Sai nghĩa', 'type': 'content'}, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Report.objects.get().user, self.user)
        res = self.client.post('/api/reports/', {'target_type': 'sense', 'target_id': target, 'reason': 'Nghĩa sai'}, format='json')
        self.assertEqual(res.status_code, 201)  # báo sai 1 nghĩa cụ thể
        for body in ({'target_type': 'x', 'target_id': target, 'reason': 'a'}, {'target_type': 'word', 'target_id': 'no', 'reason': 'a'},
                     {'target_type': 'word', 'target_id': target, 'reason': ' '}, {'target_type': 'word', 'target_id': target, 'reason': 'a', 'type': 'zz'}):
            self.assertEqual(self.client.post('/api/reports/', body, format='json').status_code, 400)

        self.assertEqual(self.client.post('/api/feedback/', {'message': 'Thêm chế độ tối'}, format='json').status_code, 201)
        self.assertEqual(Feedback.objects.get().type, 'feature')
        self.assertEqual(self.client.post('/api/feedback/', {'message': ''}, format='json').status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post('/api/feedback/', {'message': 'x'}, format='json').status_code, 401)
