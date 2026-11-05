import math

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Collection
from core.scores import refresh_collection_scores


class CollectionScoreTests(TestCase):
    def setUp(self):
        self.bob = User.objects.create_user('bob')

    def make(self, **kwargs):
        return Collection.objects.create(name='c', language_code='en', released=True, created_by=self.bob, item_count=10, **kwargs)

    def score(self, collection):
        refresh_collection_scores(pk=collection.pk)
        return Collection.objects.get(pk=collection.pk).score

    def test_each_signal_adds_in_log_scale(self):
        self.assertEqual(self.score(self.make()), 0)
        self.assertEqual(self.score(self.make(like_count=1)), round(100 * math.log(2)))
        self.assertEqual(self.score(self.make(remix_count=3)), round(80 * math.log(4)))
        self.assertEqual(self.score(self.make(view_count=100)), round(10 * math.log(101)))
        # Cực hot không đè hết: gấp 100 lần lượt thích chỉ hơn ~6.6 lần điểm của 1 lượt thích
        self.assertLess(self.score(self.make(like_count=100)), 8 * self.score(self.make(like_count=1)))

    def test_official_bonus_and_small_set_penalty(self):
        self.assertEqual(self.score(self.make(is_official=True)), 300)
        small = Collection.objects.create(name='s', language_code='en', released=True, created_by=self.bob, item_count=2)
        self.assertEqual(self.score(small), -50)
