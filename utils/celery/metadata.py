import asyncio
from celery import shared_task

# Import đúng các model của bạn
from ai.models.AIWord import AIWord
from ai.models.AISense import AISense 
from utils.utils.socket import socket_message
from django.contrib.auth import get_user_model
from utils.ai.metadata import ai_create_metadata
from utils.redis.word_init import WordCacheManager
from utils.utils.sense_handle import serialize_entries, get_user_lang_content
from ai.serializers.AIWord import AIWordSerializer
from django.db.models import Prefetch

User = get_user_model()

from django.utils import timezone
@shared_task(bind=True, retry_backoff=True, max_retries=3)
def task_create_metadata(self,  info):
    try:
        translated = asyncio.run(ai_create_metadata(info))

        instances = []
        for item in translated:
            # Giả sử item là {'id': 1, 'contents': {...}}
            obj = AISense(pk=item['id'], contents=item['contents'], updated_at=timezone.now())
            instances.append(obj)

        # Bây giờ bulk_update sẽ chạy ngon lành
        AISense.objects.bulk_update(instances, fields=['contents', 'updated_at'])
        # print('translated', translated)

        # 3. Prefetch Senses
        cache = WordCacheManager()
        word_id = info['word_id']
        word_value = info['word_value']
        language_code = info['language_code']
        user_language_code = info['user_language_code']

        sense_qs = AISense.objects.filter(is_official=True).select_related('metadata', 'previous').order_by('-updated_at')

        # 4. Gộp vào query chính
        word_instance = AIWord.objects.filter(value=word_value, language_code=language_code).prefetch_related(
            Prefetch('senses', queryset=sense_qs, to_attr='prefetched_senses')
        ).first()
        
        senses_instance = word_instance.prefetched_senses # Thay vì .senses.all()

        socket_room = 'test'

        entries = serialize_entries(senses_instance)
        word_instance.processed_entries = entries

        data = AIWordSerializer(word_instance).data

        cache.cache_word(language_code=language_code, word_id=word_id, word_val=word_value, data=data.get('senses'))

        user_lang_content = get_user_lang_content(language_code, user_language_code, data)

        # socket

        asyncio.run(socket_message(socket_room, {"type": "FULL_SENSE",
                                "payload": user_lang_content}))
    except Exception as e:
        print(e)
        pass
        # raise self.retry(exc=e, countdown=5)