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

    def test_learning_fields(self):
        # Trường phần học: cấp thẻ 0-7, FSRS đủ trường, review_log ghi cả bước trong phiên, phiên có cấu hình + bộ đếm
        sid, coll = str(uuid7()), str(uuid7())
        out = self.push(user_sense_progress=[
            {'original_sense_id': sid, 'level': 0, 'max_level': 3, 'scheduled_days': 2.5, 'learning_steps': 1},
            {'original_sense_id': str(uuid7()), 'level': 8},
        ]).data['results']['user_sense_progress']
        self.assertEqual((out['accepted'], len(out['rejected'])), (1, 1))
        progress = UserSenseProgress.objects.get(original_sense_id=sid)
        self.assertEqual((progress.level, progress.max_level, progress.scheduled_days, progress.learning_steps), (0, 3, 2.5, 1))

        rid = str(uuid7())
        self.push(review_log=[{'id': rid, 'original_sense_id': sid, 'rating': 2, 'mode': 'learn:cloze', 'is_fsrs': 0,
                               'level': 1, 'hint_count': 1, 'attempts': 2, 'reviewed_at': iso(-1)}])
        log = ReviewLog.objects.get(pk=rid)
        self.assertEqual((log.mode, log.is_fsrs, log.level, log.hint_count, log.attempts), ('learn:cloze', False, 1, 1, 2))

        session = {'id': str(uuid7()), 'type': 'learn', 'status': 'in_progress', 'collection_id': coll, 'start_at': iso(-10),
                   'methods': '["write", "cloze"]', 'new_count': 6, 'review_count': 0, 'correct_count': 30, 'wrong_count': 4, 'xp_earned': 60}
        self.assertEqual(self.push(learn_session=[session]).data['results']['learn_session']['accepted'], 1)
        row = LearnSession.objects.get(pk=session['id'])
        self.assertEqual((row.status, str(row.collection_id), row.methods, row.new_count, row.xp_earned),
                         ('in_progress', coll, ['write', 'cloze'], 6, 60))
        res = self.push(learn_session=[dict(session, id=str(uuid7()), status='x'), dict(session, id=str(uuid7()), methods=[1]),
                                       dict(session, id=str(uuid7()), collection_id='x')])
        self.assertEqual(len(res.data['results']['learn_session']['rejected']), 3)

        pulled = self.client.get('/api/sync/pull/', {'tables': 'learn_session,review_log,user_sense_progress'}).data['tables']
        self.assertIn('learning_steps', pulled['user_sense_progress']['items'][0])
        self.assertTrue(pulled['review_log']['items'][0]['is_fsrs'] is False)
        self.assertEqual(pulled['learn_session']['items'][0]['methods'], ['write', 'cloze'])

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


class ReviewStatsTests(SyncTestBase):
    """Thống kê chung của nghĩa cộng dồn khi push review_log (tracking/stats.py)."""

    def log(self, rating, minutes=0, is_fsrs=True, sense=None):
        return {'id': str(uuid7()), 'original_sense_id': str((sense or self.sense).id), 'rating': rating,
                'is_fsrs': is_fsrs, 'reviewed_at': iso(minutes)}

    def stats(self, sense=None):
        sense = sense or self.sense
        sense.refresh_from_db()
        return sense.global_forget_count, sense.global_remember_count, sense.learner_count, round(sense.global_difficulty, 3)

    def test_first_rating_sets_difficulty_and_resend_is_not_counted(self):
        from tracking.fsrs import initial_difficulty
        custom = AISense.objects.create(word=self.sense.word, word_value='bank', language_code='en', original=self.sense, is_official=False, contents={})
        logs = [self.log(3, -10), self.log(1, -5), self.log(3, -1, is_fsrs=False)]
        self.assertEqual(self.push(review_log=logs).status_code, 200)
        good = round(initial_difficulty(3), 3)
        self.assertEqual(self.stats(), (1, 1, 1, good))  # bước trong phiên (is_fsrs=0) không tính
        self.assertEqual(self.stats(custom), (1, 1, 1, good))  # mọi bản cùng original mang cùng số liệu

        self.push(review_log=logs)  # gửi lại khi mạng chập chờn
        self.assertEqual(self.stats(), (1, 1, 1, good))

        self.client.force_authenticate(self.other)
        self.push(review_log=[self.log(1)])
        self.assertEqual(self.stats(), (2, 1, 2, round((initial_difficulty(3) + initial_difficulty(1)) / 2, 3)))

    def test_only_first_review_of_a_user_counts_as_learner(self):
        self.push(review_log=[self.log(4, -60)])
        self.push(review_log=[self.log(2, -1), self.log(3)])
        forget, remember, learners, _ = self.stats()
        self.assertEqual((forget, remember, learners), (0, 3, 1))

    def test_initial_difficulty_follows_fsrs(self):
        from tracking.fsrs import initial_difficulty
        values = [initial_difficulty(r) for r in (1, 2, 3, 4)]
        self.assertEqual(values, sorted(values, reverse=True))  # Again khó nhất
        self.assertTrue(all(1 <= v <= 10 for v in values))

    def test_pull_user_collections(self):
        from core.models import UserCollection
        collection = Collection.objects.create(name='C', language_code='en', is_official=True)
        mine = UserCollection.objects.create(user=self.user, collection=collection, version=2)
        UserCollection.objects.create(user=self.other, collection=collection)
        rows = self.client.get('/api/sync/pull/', {'tables': 'user_collection'}).data['tables']['user_collection']['items']
        self.assertEqual([(str(r['id']), str(r['collection_id']), r['version']) for r in rows], [(str(mine.id), str(collection.id), 2)])


class LearnTestBase(SyncTestBase):
    """Bộ 5 nghĩa (cat có 2 nghĩa) đã tải; dùng cho GET /api/learn/new/ và /api/learn/review/ (tracking/learn.py)."""

    def setUp(self):
        super().setUp()
        from core.models import CollectionItem, UserCollection
        self.collection = Collection.objects.create(name='C', language_code='en', is_official=True, version=1)
        self.senses = {}
        cat = AIWord.objects.create(value='cat', language_code='en', status='COMPLETED')
        for name, word in [('cat1', cat), ('cat2', cat), ('dog', None), ('bird', None), ('fish', None)]:
            word = word or AIWord.objects.create(value=name, language_code='en', status='COMPLETED')
            sense = AISense.objects.create(word=word, word_value=word.value, language_code='en', contents={'definition': {'en': {'value': name}}})
            self.senses[name] = sense
            CollectionItem.objects.create(collection=self.collection, sense=sense, original=sense, released=True, version=1,
                                          value=word.value, display_order=len(self.senses))
        UserCollection.objects.create(user=self.user, collection=self.collection, version=1)

    def progress(self, sense, **kwargs):
        defaults = {'state': 2, 'repetitions': 3, 'stability': 5, 'level': 3,
                    'last_reviewed_at': timezone.now() - timedelta(days=5), 'next_review_at': timezone.now() - timedelta(hours=1)}
        sense = self.senses[sense] if isinstance(sense, str) else sense
        return UserSenseProgress.objects.create(user=self.user, original_sense_id=sense.id, **{**defaults, **kwargs})

    def get(self, path, **params):
        return self.client.get(f'/api/learn/{path}/', {'user_lang': 'en', **params})

    def ids(self, cards):
        return [str(c['sense_id']) for c in cards]

    def sid(self, *names):
        return [str(self.senses[n].id) for n in names]


class LearnNewTests(LearnTestBase):
    def new(self, **params):
        return self.get('new', collection_id=str(self.collection.id), **params)

    def test_follow_collection_order_one_sense_per_word(self):
        data = self.new(count=2).data
        self.assertEqual(self.ids(data['new']), self.sid('cat1', 'dog'))  # cat2 cùng từ, sang phiên sau
        self.assertEqual(data['new_available'], 5)
        self.assertEqual({str(s['id']) for s in data['senses']}, set(self.ids(data['new'])))
        self.assertEqual(len(data['words']), 2)

    def test_learned_and_hidden_senses_are_skipped(self):
        from interact.models import UserInteraction
        self.progress('dog')
        self.progress('bird', state=0, repetitions=0)  # có dòng tiến độ nhưng chưa học lần nào: vẫn là mới
        UserInteraction.objects.create(user=self.user, target_type='sense', target_id=self.senses['cat1'].id, interaction_type='hide', status=True)
        data = self.new(count=10).data
        self.assertEqual(self.ids(data['new']), self.sid('cat2', 'bird', 'fish'))
        self.assertEqual(data['new_available'], 3)

    def test_validation(self):
        self.assertEqual(self.get('new').status_code, 400)  # thiếu collection_id
        self.assertEqual(self.new(count='x').status_code, 400)
        self.assertEqual(self.new(mode='quick').status_code, 400)  # học mới chưa có kiểu nhanh
        self.client.force_authenticate(self.other)
        self.assertEqual(self.new().status_code, 404)  # chưa tải bộ
        self.client.force_authenticate(None)
        self.assertEqual(self.new().status_code, 401)


class LearnReviewTests(LearnTestBase):
    def test_lowest_recall_first_and_count(self):
        self.progress('fish', stability=1)
        self.progress('bird', stability=30)
        self.progress('dog', stability=10)
        self.progress('cat1', next_review_at=timezone.now() + timedelta(days=3))  # chưa đến hạn
        self.progress('cat2', is_hidden=True)
        data = self.get('review', count=2).data
        self.assertEqual(self.ids(data['due']), self.sid('fish', 'dog'))
        self.assertLess(data['due'][0]['retrievability'], data['due'][1]['retrievability'])
        self.assertEqual((data['due_count'], data['due_total'], data['mode']), (3, 3, 'normal'))
        self.assertEqual(len(data['senses']), 2)

    def test_quick_mode_only_cards_with_enough_level(self):
        self.progress('fish', level=1)
        self.progress('bird', level=3)
        UserSetting.objects.create(user=self.user, key='swipe_min_level', value=3)
        data = self.get('review', mode='quick').data
        self.assertEqual(self.ids(data['due']), self.sid('bird'))
        self.assertEqual((data['due_count'], data['due_total']), (1, 2))
        self.assertEqual(self.ids(self.get('review').data['due']), self.sid('fish', 'bird'))  # normal không lọc cấp

    def test_all_senses_or_one_collection(self):
        stray_word = AIWord.objects.create(value='owl', language_code='en', status='COMPLETED')
        stray = AISense.objects.create(word=stray_word, word_value='owl', language_code='en', contents={})
        custom = AISense.objects.create(word=stray_word, word_value='owl', language_code='en', original=stray,
                                        is_official=False, created_by=self.user, contents={})
        self.progress(stray, stability=0.5)
        self.progress('fish')
        everywhere = self.get('review').data
        self.assertEqual(self.ids(everywhere['due']), [str(custom.id), *self.sid('fish')])  # nghĩa ngoài bộ: dùng bản riêng của user
        self.assertIsNone(everywhere['due'][0]['item_id'])
        in_collection = self.get('review', collection_id=str(self.collection.id)).data
        self.assertEqual(self.ids(in_collection['due']), self.sid('fish'))
        self.assertIsNotNone(in_collection['due'][0]['item_id'])

    def test_due_until_next_day_start_in_user_zone(self):
        from tracking.learn import day_ends_at
        UserSetting.objects.create(user=self.user, key='day_start_hour', value=4)
        cutoff = day_ends_at(self.user)
        self.assertEqual(cutoff.astimezone(ZoneInfo('Asia/Ho_Chi_Minh')).hour, 4)
        self.assertTrue(timezone.now() < cutoff <= timezone.now() + timedelta(days=1))
        self.progress('fish', next_review_at=cutoff - timedelta(minutes=1))
        self.progress('bird', next_review_at=cutoff + timedelta(minutes=1))
        self.assertEqual(self.ids(self.get('review').data['due']), self.sid('fish'))

    def test_validation(self):
        self.assertEqual(self.get('review', mode='bogus').status_code, 400)
        self.assertEqual(self.get('review', collection_id='x').status_code, 400)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.get('review', collection_id=str(self.collection.id)).status_code, 404)
        self.assertEqual(self.get('review').data['due'], [])
