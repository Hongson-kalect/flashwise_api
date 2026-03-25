import asyncio
import requests
import os
import io
from PIL import Image
from django.core.files.base import ContentFile
from celery import shared_task
from django.db import transaction
from asgiref.sync import async_to_sync

# Import đúng các model của bạn
from ai.models.AIWord import AIWord
from core.models.ImageLibrary import ImageContext, ImageLibrary, ImageLibraryContext
from ai.models.AISense import AISense 
from utils.utils.image_compress import save_sense_image
from utils.utils.socket import socket_message
from django.contrib.auth import get_user_model
from utils.ai.translate import render_translate

User = get_user_model()


@shared_task(bind=True, retry_backoff=True, max_retries=3)
def task_create_translate(self, word, senses, user_languages):
    print('vào chỗ lấy image')
    """Task xử lý ảnh: Check Context -> Fetch Pixabay -> Save Local -> Link
        word: {id, value, language_code}
    """

    contents = {}
    need_translation = {}

    for sense_id, sense in senses.items():
        data = {}
        for key, content in sense.items():
            if key == 'translates':
                data['translates'] = {}
                for item in content:
                    data["translates"][item.get('id')] = item.get('value')

            data[key] = content.get('value')

        contents[sense_id] = data
    try:
        asyncio.run(render_translate(word, contents, user_languages))
    except Exception as e:
        raise self.retry(exc=e, countdown=5)
