"""Worker gom từ AI vừa tạo xong vào bộ (utils/celery/worker.py execute_bulk_db)."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from ai.models import AISense, AIWord
from core.models import Collection, CollectionItem, UserCollection
from core.queries import live_items
from utils.celery.worker import BUFFER_CONFIGS, execute_bulk_db

User = get_user_model()
ITEMS_CONFIG = next(c for c in BUFFER_CONFIGS if c['key'] == 'db_buffer:collectionItem:create')


class CollectionItemsFlushTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user('author')
        self.collection = Collection.objects.create(name='C', language_code='en', created_by=self.author)
        UserCollection.objects.create(user=self.author, collection=self.collection)
        word = AIWord.objects.create(value='umbrella', language_code='en', status='COMPLETED')
        self.sense = AISense.objects.create(word=word, word_value='umbrella', language_code='en', is_official=True, contents={})
        first = AIWord.objects.create(value='bank', language_code='en', status='COMPLETED')
        self.first = AISense.objects.create(word=first, word_value='bank', language_code='en', is_official=True, contents={})
        CollectionItem.objects.create(collection=self.collection, sense=self.first, original=self.first, value='bank', display_order=3, created_by=self.author)

    def flush(self):
        return execute_bulk_db([{'collection_id': str(self.collection.id), 'value': 'umbrella', 'sense_id': str(self.sense.id)}], ITEMS_CONFIG)

    def test_added_item_is_the_authors_draft_at_the_end_and_visible(self):
        self.assertEqual(self.flush(), 1)
        item = CollectionItem.objects.get(collection=self.collection, sense=self.sense)
        self.assertEqual((item.created_by_id, item.original_id, item.display_order, item.value), (self.author.id, self.sense.id, 4.0, 'umbrella'))
        visible = [str(i.sense_id) for i in live_items(self.collection.id, self.collection.version, self.author)]
        self.assertEqual(visible, [str(self.first.id), str(self.sense.id)])  # tác giả thấy được (trước đây dòng không có người tạo thì không ai thấy)
        self.assertEqual([i.sense_id for i in live_items(self.collection.id, self.collection.version, User.objects.create_user('other'))], [])

    def test_same_word_twice_does_not_duplicate(self):
        self.flush()
        self.assertEqual(self.flush(), 0)
        self.assertEqual(CollectionItem.objects.filter(collection=self.collection, sense=self.sense).count(), 1)

    def test_unknown_collection_is_ignored(self):
        job = {'collection_id': '00000000-0000-7000-8000-000000000000', 'value': 'umbrella', 'sense_id': str(self.sense.id)}
        self.assertEqual(execute_bulk_db([job], ITEMS_CONFIG), 0)
