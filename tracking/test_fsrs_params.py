"""Tham số FSRS riêng của user (tracking/fsrs_params.py) và luật "đã biết" (kiểm tra sau 1 ngày, không ôn vuốt khi chưa qua nhớ lại)."""
import random
from datetime import datetime, timedelta, timezone as dt_timezone

import fsrs_rs_python as rs
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from fsrs import Card, Rating, Scheduler
from rest_framework.test import APITestCase
from uuid6 import uuid7

from tracking.fsrs import DEFAULT_W, PARAMS_KEY, PARAMS_META_KEY, user_parameters, valid_parameters
from tracking.fsrs_params import MIN_ITEMS, fit_user
from tracking.models import ReviewLog
from tracking.session import scheduler_for
from user.models import UserProfile, UserSetting

User = get_user_model()
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Người học "thật": quên nhanh hơn mặc định nhiều (độ dốc w20 cao, tăng độ bền sau khi nhớ thấp)
TRUE_W = list(DEFAULT_W)
TRUE_W[20] = 0.6
TRUE_W[8] = 0.8
TRUE_W[11] = 0.8


def simulate(user, cards=300, seed=7):
    """Ghi review_log của 1 người học mô phỏng: lịch ôn theo tham số mặc định, kết quả nhớ / quên theo TRUE_W."""
    rng = random.Random(seed)
    scheduler = Scheduler(parameters=tuple(DEFAULT_W), enable_fuzzing=False)
    truth = Scheduler(parameters=tuple(TRUE_W), enable_fuzzing=False)
    start = datetime(2026, 1, 1, 8, tzinfo=dt_timezone.utc)
    logs = []
    for i in range(cards):
        sense_id = uuid7()
        card, real, now = Card(), Card(), start + timedelta(days=rng.randint(0, 80))  # card: lịch app xếp; real: trí nhớ thật của người học
        for k in range(10):
            if k == 0:
                rating = Rating.Good
            else:
                recalled = rng.random() < truth.get_card_retrievability(real, now)
                rating = rng.choice([Rating.Good, Rating.Good, Rating.Easy, Rating.Hard]) if recalled else Rating.Again
            logs.append(ReviewLog(user=user, original_sense_id=sense_id, rating=int(rating), is_fsrs=True, reviewed_at=now))
            card, _ = scheduler.review_card(card, rating, now)
            real, _ = truth.review_card(real, rating, now)
            now = max(card.due, now + timedelta(hours=1))
    ReviewLog.objects.bulk_create(logs)
    return len(logs)


class ParametersStorageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('alice', password='x')
        UserProfile.objects.get_or_create(user=self.user)

    def test_validation(self):
        self.assertTrue(valid_parameters(list(DEFAULT_W)))
        self.assertFalse(valid_parameters(list(DEFAULT_W)[:20]))
        self.assertFalse(valid_parameters([float('nan')] * 21))
        self.assertFalse(valid_parameters([*DEFAULT_W[:20], 2.0]))  # w20 ngoài khoảng
        self.assertFalse(valid_parameters('x'))

    def test_default_when_missing_or_invalid(self):
        self.assertEqual(user_parameters(self.user), DEFAULT_W)
        UserSetting.objects.create(user=self.user, key=PARAMS_KEY, value=[1, 2, 3])
        self.assertEqual(user_parameters(self.user), DEFAULT_W)

    def test_custom_parameters_drive_the_scheduler(self):
        custom = list(DEFAULT_W)
        custom[20] = 0.3
        UserSetting.objects.create(user=self.user, key=PARAMS_KEY, value=custom)
        self.assertEqual(user_parameters(self.user), tuple(custom))
        self.assertEqual(tuple(scheduler_for(self.user, 'normal').parameters), tuple(custom))


class FitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('alice', password='x')
        UserProfile.objects.get_or_create(user=self.user)

    def test_not_enough_data_keeps_default(self):
        simulate(self.user, cards=20)
        result = fit_user(self.user)
        self.assertEqual(result['status'], 'not_enough')
        self.assertEqual(user_parameters(self.user), DEFAULT_W)
        self.assertFalse(UserSetting.objects.filter(user=self.user, key=PARAMS_KEY).exists())

    def test_fit_improves_on_held_out_cards_and_is_saved(self):
        simulate(self.user)
        result = fit_user(self.user)
        self.assertEqual(result['status'], 'updated', result)
        self.assertGreater(result['n_items'], MIN_ITEMS)
        self.assertLess(result['loss_after'], result['loss_before'])
        params = user_parameters(self.user)
        self.assertNotEqual(params, DEFAULT_W)
        self.assertGreater(params[20], DEFAULT_W[20])  # học ra được: quên dốc hơn mặc định
        meta = UserSetting.objects.get(user=self.user, key=PARAMS_META_KEY).value
        self.assertEqual(meta['status'], 'updated')
        # chưa có thêm dữ liệu thì không tối ưu lại
        self.assertEqual(fit_user(self.user)['status'], 'up_to_date')
        self.assertEqual(fit_user(self.user, force=True)['status'] in ('updated', 'kept'), True)

    def test_already_good_parameters_are_kept(self):
        """Dữ liệu sinh đúng bằng tham số mặc định: tối ưu không có gì tốt hơn, giữ nguyên."""
        global TRUE_W
        saved = TRUE_W
        TRUE_W = list(DEFAULT_W)
        try:
            simulate(self.user)
        finally:
            TRUE_W = saved
        result = fit_user(self.user)
        self.assertIn(result['status'], ('kept', 'updated'))
        if result['status'] == 'kept':
            self.assertEqual(user_parameters(self.user), DEFAULT_W)

    def test_same_day_reviews_do_not_break_items(self):
        sid = uuid7()
        now = datetime(2026, 3, 1, 9, tzinfo=dt_timezone.utc)
        ReviewLog.objects.bulk_create([ReviewLog(user=self.user, original_sense_id=sid, rating=3, is_fsrs=True, reviewed_at=now + timedelta(hours=h)) for h in (0, 1, 2)])
        self.assertEqual(fit_user(self.user)['status'], 'not_enough')


@override_settings(CACHES=LOCMEM_CACHE)
class EndpointTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('alice', password='x')
        UserProfile.objects.get_or_create(user=self.user)
        self.client.force_authenticate(self.user)

    def test_get_returns_defaults(self):
        res = self.client.get('/api/learn/fsrs/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['parameters'], list(DEFAULT_W))

    def test_post_fits_and_is_throttled(self):
        simulate(self.user)
        res = self.client.post('/api/learn/fsrs/', {}, format='json')
        self.assertEqual((res.status_code, res.data['status']), (200, 'updated'))
        self.assertNotEqual(res.data['parameters'], list(DEFAULT_W))
        for _ in range(5):
            self.client.post('/api/learn/fsrs/', {}, format='json')
        self.assertEqual(self.client.post('/api/learn/fsrs/', {}, format='json').status_code, 429)

    def test_requires_login(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/learn/fsrs/').status_code, 401)
