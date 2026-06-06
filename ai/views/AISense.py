import copy
import json
import redis
from django.db import transaction, models
from urllib.parse import unquote
import asyncio
from importlib import metadata
import threading
import django.utils.timezone as timezone
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticatedOrReadOnly, AllowAny
from ai.models.AISense import AISense
from ai.models.AISenseContent import AISenseContent
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.TranslateLog import TranslateLog
from ai.serializers.AISense import AISenseSerializer
from ai.serializers.AISenseContent import AISenseContentSerializer
from ai.views.AIWord import detect_missing_content
from ai.models.AIWord import AIWord
from utils.utils import uuidv7
from utils.utils.ai import ai_create_new_word, render_translate
from utils.utils.background_task import background_task
from utils.utils.flatten_id import flatten_ids_by_langs
from utils.utils.limit_prefetch import limit_prefetch
from utils.utils.sense_content_tree import patch_and_clone_contents
from utils.utils.sense_handle import serialize_entries, serialize_senses, deep_merge
from utils.utils.socket import get_safe_room_id, run_async_task, socket_close
from ai.serializers.AISenseMetadata import AISenseMetadataSerializer
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from django.contrib.auth.models import User
from utils.utils.sense_handle import get_user_lang_sense

from django.db.models import F, ExpressionWrapper, FloatField, Func
from django.db.models.functions import Cast, Now


def save_value(type, content, bulk, content_json, language, user_language, user):
    obj = {}
    id =uuidv7.generate_uuid7()
    try:
        value = content['value']
        translate = content.get('translate',None)
        if value:
            obj[language] = str(id)
            bulk.append(AISenseContent(value=value, id=id, type=type, created_by=user, language_code=language))

            if translate:
                t_id = uuidv7.generate_uuid7()
                obj[user_language] = str(t_id)
                bulk.append(AISenseContent(value=translate, id=t_id, created_by=user, language_code=user_language))
            
            if type == 'examples':
                content_json.setdefault(type, []).append(obj)
            else:
                content_json[type] = obj
    except Exception as e:
        return Response(status=status.HTTP_400_BAD_REQUEST, data={'message': 'Bad request content'})
        

def save_contents(contents, language, user_language, user):
    bulk=[]
    content_json={}

    for type, content in contents.items():
        if type =='definition' or type == 'usage':
            save_value(type, content, bulk, content_json,  language, user_language, user)
        elif type == 'translations':
            save_value(type, {"value": content, "translate": None}, bulk, content_json, language, user_language, user)
        elif type == 'examples':
            for item in content:
                save_value('examples', item, bulk, content_json, language, user_language, user)

    return [bulk, content_json]

class AISenseViewSet(SoftDeleteViewSet):
    queryset = AISense.objects.select_related('metadata', 'original').all()
    

    
    serializer_class = AISenseSerializer
    # permission_classes = [IsAuthenticatedOrReadOnly]
    permission_classes = [AllowAny] # testing

    def retrieve(self, request, *args, **kwargs):
        sense = self.get_object()  # lấy object theo id
        user_language_code = request.query_params.get('user_lang', 'en')

        sense.contents, missing = get_user_lang_sense(sense.language_code, user_language_code, sense.contents or sense.original.contents, sense.id)

        if missing:
            try:
                r_queue = redis.Redis(host='redis', port=6379, db=0)

                # Đẩy vào queue "redis_word"
                r_queue.rpush("redis_trans", json.dumps({
                    "word_id": str(sense.word_id),
                    "language_code": sense.language_code,
                    "user_language_code": user_language_code,
                    "missing_translate": [missing],
                    'current_senses':[sense]
                }))
            except:
                pass

        serializer = self.get_serializer(sense)
        return Response(serializer.data)

        # data = AISenseSerializer(sense).data

        # return Response({'detail': 'Sense updated.', 'status': '200', 'data': data}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='create-sense')
    def create_sense(self, request, pk=None, *args, **kwargs):
        # params: word_id, user_lang, contents{definition{value, translate}, usage{}, examples[{}], translations[]}, metadata

        # user = request.user
        user = User.objects.get_or_create(username='admin', password='admin', email='admin@gmail.com', is_superuser=True)[0]
        word_id = request.data.get('word_id')
        user_language_code = request.data.get('user_lang')
        contents = request.data.get('contents')
        metadata = request.data.get('metadata')

        if not word_id or not user_language_code or not contents or not metadata:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing required parameters"})
        
        # check lượt, quyền hạn của user
        # flow: get word
        word_instance = AIWord.objects.filter(id=word_id).first()
        if not word_instance:
            return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "Word not found"})
        
        if not contents.get('definition',{}).get(word_instance.language_code,{}).get('value'):
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing definition"})

        # socket_room = get_safe_room_id(word_instance.value, user_language_code, user_language_code)+str(user.id)
        # socket_room = get_safe_room_id(word_instance.value, word_instance.language_code) 

        with transaction.atomic():
            # save contents

            # [content_bulk, sense_content] = save_contents(contents, word_instance.language_code, user_language_code, user)

            # contents = AISenseContent.objects.bulk_create(content_bulk)

            # save metadata
            metadata = AISenseMetadata(**metadata)
            metadata.save()

            # save sense
            sense_instance = AISense(word=word_instance, word_value = word_instance.value, metadata=metadata, is_frozen=None, contents=contents, created_by=user, language_code=word_instance.language_code, is_official=False, is_ai_created=False)
            sense_instance.save()

            sense = {
            'id': str(sense_instance.id),
            "contents": sense_instance.contents,
            'metadata': AISenseMetadataSerializer(sense_instance.metadata).data if sense_instance.metadata else None,
            "preview": sense_instance.preview
        }

            # [sense_json] = serialize_senses([sense], contents, word_instance.language_code, user_language_code)
            print(word_instance.language_code, user_language_code, sense['contents'], sense['id'])
            sense['contents'], missing_contents = get_user_lang_sense(word_instance.language_code, user_language_code, sense['contents'], sense['id'])

            data = AISenseSerializer(sense_instance).data
            if missing_contents:
                    # Unique (word, user_language_code with 1 status PROCESSING allowed)
                try:
                    r_queue = redis.Redis(host='redis', port=6379, db=0)
                    r_queue.rpush("redis_trans", json.dumps({
                        "word_id": str(word_id),
                        "language_code": word_instance.language_code,
                        "user_language_code": user_language_code,
                        "missing_translate":[missing_contents],
                        'current_senses':[sense]
                    }))
                except:
                    pass

                # Keep connect with socket to get result
                return Response({'detail': 'Word incomplete.', 'status': '206', 'data': data}, status=status.HTTP_206_PARTIAL_CONTENT)

            return Response({'detail': 'Sense created.', 'status': '201', 'data': data}, status=status.HTTP_201_CREATED)

            
            # data = AISenseSerializer(sense_json).data

            # [missing_contents, have_translated] = detect_missing_content([sense], contents, word_instance.language_code, user_language_code)


            # save info to return to client to get id to make modify funtion asap
            
            # client create socket connect with word(md5)+language+user_language
            if missing_contents:
                # --background task--
                # -> check translate 
                # -> get translate and metadata
                # -> bulk create contents + metadata
                # -> return data via socket
                translate_instance = TranslateLog.objects.create(word=word_instance, language_code=user_language_code, status="PROCESSING", type="CREATE NEW SENSE")
                background_task(render_translate(user, translate_instance, word_instance.value, [sense], missing_contents , have_translated, word_instance.language_code, user_language_code, socket_room))

                return Response({'detail': 'PROCESSING', 'status': '206', 'data': data}, status=status.HTTP_206_PARTIAL_CONTENT)
            else:
                return Response({'detail': 'PROCESSING', 'status': '200', 'data': data}, status=status.HTTP_200_OK)

        return Response(status=status.HTTP_400_BAD_REQUEST, data={'message': 'Bad request'})
    
    @action(detail=False, methods=['put'], url_path='update-sense')
    def update_sense(self, request, *args, **kwargs):
        sense_id = request.data.get('sense_id')
        contents = request.data.get('contents', {})  # [{id, value}]
        delta = request.data.get('delta', {})  # object
        metadata = request.data.get('metadata', None)
        user_language_code = request.data.get('user_lang')
        user = request.user if request.user.is_authenticated else User.objects.first()

        changes=[]

        # 1. Lấy sense

        sense = AISense.objects.filter(id=sense_id).select_related('original','metadata').first()

        if not sense:
            return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "Sense not found"})

        metadata_instance = sense.metadata
        image = request.data.get('image', sense.image_preview)

        if metadata:

            # Chuyển thành dict và xử lý
            new_data = sense.metadata.__dict__.copy()
            new_data.pop('_state')  # Bắt buộc phải xóa
            new_data.pop('id')      # Xóa để tạo record mới hoàn toàn
            new_data.pop('created_at')      # Xóa để tạo record mới hoàn toàn
            new_data.pop('created_by_id')      # Xóa để tạo record mới hoàn toàn
            new_data.pop('updated_at')      # Xóa để tạo record mới hoàn toàn
            new_data.pop('updated_by_id')      # Xóa để tạo record mới hoàn toàn

            # Cập nhật các trường muốn thay đổi
            new_data.update({
                **metadata
            })

            # Tạo instance mới
            metadata_instance = AISenseMetadata.objects.create(**new_data)

        if sense.is_official:
            sense = AISense.objects.create(
                word=sense.word, 
                word_value=sense.word_value, 
                delta=delta, 
                image_preview=image,
                metadata = metadata_instance,
                created_by=user, 
                language_code=sense.language_code, 
                is_official=False, 
                original=sense.original or sense,
                is_frozen =False,
                is_ai_created=False)

        elif sense.is_frozen:
            # update của update, Đây là trường hợp ko biết content có gì, phải merge 2 delta
            # delta = deep_merge(delta, sense.delta)
            # Thực tế thì user có thể gửi full delta thay vì semi-delta. Khi user update thì sẽ trực tiếp update value của delta hiện tại
            sense =AISense.objects.create(
                word=sense.word, 
                word_value=sense.word_value, 
                delta=delta, 
                image_preview=image, 
                metadata = metadata_instance,
                created_by=user, 
                language_code=sense.language_code, 
                is_official=False, 
                is_frozen =False,
                is_ai_created=False)

        else:
            delta = deep_merge(delta, sense.delta)
            sense.delta = delta
            sense.image_preview = image
            sense.metadata = metadata_instance
            sense.save()

        sense.contents = get_user_lang_sense(sense.language_code, user_language_code, sense.contents or sense.original.contents, sense.id)

        data = AISenseSerializer(sense).data

        return Response(data, status=status.HTTP_200_OK)