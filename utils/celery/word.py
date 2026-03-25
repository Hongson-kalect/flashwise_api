import asyncio
import requests
import os
import io
from PIL import Image
from django.core.files.base import ContentFile
from celery import shared_task
from django.db import transaction

# Import đúng các model của bạn
from ai.models.AIWord import AIWord
from core.models.ImageLibrary import ImageContext, ImageLibrary, ImageLibraryContext
from ai.models.AISense import AISense 
from utils.ai.word_render import ai_create_new_word
from utils.utils.socket import socket_message
from django.contrib.auth import get_user_model

User = get_user_model()

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def ai_create_new_word_task(self, user_id, word_id, language_code, user_language_code, socket_room):
    print('start celery')
    user = User.objects.get(id=user_id)
    word = AIWord.objects.get(id=word_id)
    # Dùng asyncio.run để tạo môi trường loop cho các lệnh async bên dưới
    try:
        asyncio.run(ai_create_new_word(user, word, language_code, user_language_code, socket_room))
    except Exception as e:
        raise self.retry(exc=e, countdown=5)


