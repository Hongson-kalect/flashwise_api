import copy
import json
import time
import uuid
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
from core.models import Defination, Example, ExampleTranslate, Translate, WordForm
from ai.models.AIWord import AIWord
from django.db.models import Prefetch, Window, F, Q
from django.db.models.functions import RowNumber
from ai.serializers.AIWord import AIWordSerializer
from core.models.Language import Language
from utils.utils import uuidv7
from utils.utils.ai import ai_create_new_word, render_translate
from utils.utils.background_task import background_task
from utils.utils.flatten_id import flatten_ids_by_langs
from utils.utils.limit_prefetch import limit_prefetch
from utils.utils.sense_content_tree import patch_and_clone_contents
from utils.utils.sense_handle import serialize_entries, serialize_senses
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

        sense.contents = get_user_lang_sense(sense.language_code, user_language_code, sense.contents or sense.original.contents, sense.id)

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
        socket_room = 'test'

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
                    from utils.celery.translate import task_create_translate
                    task_create_translate.delay({
                        "word_value": word_instance.value,
                        "language_code": word_instance.language_code,
                        "user_language_code": user_language_code,
                        "missing_translate":[missing_contents],
                        'current_senses':[sense]
                    }, False)
                    # translate_instance = TranslateLog.objects.create(word=word_instance, language_code=user_language_code, status="PROCESSING")
                    # background_task(render_translate(user, translate_instance, word, senses_instance, missing_contents , need_translation, language_code, user_language_code, socket_room))
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

        sense = AISense.objects.filter(id=sense_id).first()
        if not sense:
            return Response({"message": "Sense not found"}, status=status.HTTP_404_NOT_FOUND)

        language_code = sense.language_code
        # Chuyển list contents thành map để tra cứu nhanh và tránh KeyError
        content_update_map = {str(c['id']): c['value'] for c in contents_list}

        with transaction.atomic():
            # --- CASE 1: frozen == None (Sửa trực tiếp trên bản ghi cũ) ---
            if sense.is_frozen is None:
                # Chỉ lấy những content thực sự được gửi lên để sửa
                target_contents = AISenseContent.objects.filter(id__in=content_update_map.keys())
                bulk_update_list = []
                
                for c in target_contents:
                    new_val = content_update_map.get(str(c.id))
                    if new_val is not None:
                        c.value = new_val
                        updated_at = timezone.now()
                        c.updated_at = updated_at
                        changes.append({'id': str(c.id), 'new_value': new_val})
                        bulk_update_list.append(c)
                
                if bulk_update_list:
                    AISenseContent.objects.bulk_update(bulk_update_list, fields=['value'])
                
                # Load lại data mới nhất để trả về
                final_sense = sense

            # --- CASE 2: frozen == True (Clone ra bản mới) ---
            elif sense.is_frozen is True:
                # Clone object Sense
                new_sense = copy.copy(sense)
                new_sense.pk = None # Đặt pk = None để tạo bản ghi mới
                new_sense.is_frozen = False
                new_sense.is_official=False
                new_sense.is_ai_created=False

                # Patch JSON và tạo Content mới
                # Lưu ý: Truyền content_update_map (Dict) thay vì list
                new_struct, bulk_content_list = patch_and_clone_contents(
                    struct=sense.contents,
                    update_data=content_update_map,
                    user=user,
                )

                new_ids = []
                if bulk_content_list:
                    for c in bulk_content_list:
                        new_ids.append(str(c.id))
                        changes.append({'id': str(c.id),
                                    'new_value': c.value, 
                                    'language_code': c.language_code, 
                                    'created_at': c.created_at, 
                                    'updated_at': c.updated_at, 
                                    'updated_by': user})
                    AISenseContent.objects.bulk_create(bulk_content_list)

                new_sense.contents = new_struct
                new_sense.updated_by = user
                new_sense.news = new_ids

                print('nnnnnn',new_sense.contents)
                print('nnnnnn',changes)
                
                previous_id = str(sense.id)
                new_sense.is_frozen = False
                new_sense.previous_id = previous_id
                if sense.original:
                    new_sense.original_id = sense.original_id
                else:
                    new_sense.original_id = previous_id
                new_sense.origins = sense.origins + [previous_id]
                new_sense.save()
                final_sense = new_sense

            elif sense.is_frozen is False:
                free_update = set(sense.news or [])
                bulk_update_list = []

                update_map= {}

                target_contents = AISenseContent.objects.filter(id__in=list(content_update_map.keys()))

                for c in target_contents:
                    id = str(c.id)
                    new_val = content_update_map.get(id)
                    if new_val is not None:
                        if id in free_update:
                            c.value = new_val
                            now = timezone.now()
                            c.updated_at = now
                            c.updated_by = user
                            changes.append({'id': str(c.id), 'value': new_val, 'updated_at': now, 'updated_by': user})
                            bulk_update_list.append(c)
                        else:
                            update_map[id] = new_val

                new_struct, bulk_create_list = patch_and_clone_contents(
                    struct=sense.contents,
                    update_data=update_map,
                    user=user
                )

                if bulk_update_list:
                    AISenseContent.objects.bulk_update(bulk_update_list, fields=['value'])
                if bulk_create_list:
                    # changes.extend(bulk_create_list)
                    changes.extend([{'id': str(c.id),
                                    'new_value': c.value, 
                                    'language_code': c.language_code, 
                                    'created_at': c.created_at, 
                                    'updated_at': c.updated_at, 
                                    'updated_by': user}
                                 for c in bulk_create_list])
                    AISenseContent.objects.bulk_create(bulk_create_list)

                sense.contents = new_struct
                sense.news = list(free_update.union(set([str(b.id) for b in bulk_create_list])))

                print('ssssss',bulk_create_list)
                print('ssssss',sense.news)
                print('ssssss',sense.contents)
                sense.save()
                final_sense = sense

            # --- CHUẨN HÓA DATA TRẢ VỀ (Dùng chung cho cả 3 case) ---
            # Lấy lại tất cả content (cả cũ lẫn mới) để serialize
            all_content_ids = flatten_ids_by_langs(final_sense.contents, [language_code, user_language_code])
            all_content_instances = AISenseContent.objects.filter(id__in=all_content_ids)

            print( final_sense.contents)

            [return_data] = serialize_senses(
                [final_sense],
                all_content_instances,
                language_code,
                user_language_code
            )

            data = AISenseSerializer(return_data).data

            return Response({
                'detail': 'Update successful',
                'is_cloned': sense.is_frozen is True,
                'data': data,
                'changes': changes
            }, status=status.HTTP_200_OK)
        return Response({"message": "Bad request"}, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['get'], url_path='get-ai-word')
    def get_ai_word(self, request, pk=None, *args, **kwargs):
        queryset = self.get_queryset()
        value = request.query_params.get('value')
        lang = request.query_params.get('lang')
        user_lang = request.query_params.get('user_lang')

        example_trans_prefetch = limit_prefetch(
            'translated_examples',
            ExampleTranslate.objects.filter(language_code__in=[lang,user_lang],),
            '-score',2)

        example_prefetch = limit_prefetch(
            'defination_examples',
            Example.objects.filter(language_code__in=[lang,user_lang], is_deleted=False, is_active=True),
            '-score',2,example_trans_prefetch)

        defi_prefetch = limit_prefetch(
            'definations',
            Defination.objects.filter(language_code__in=[lang,user_lang], is_deleted=False, is_active=True), 
            '-score',10, example_prefetch)
        
        form_prefetch = limit_prefetch(
            'word_forms',
             WordForm.objects.all(),
            'value',4)
        
        trans_prefetch = limit_prefetch(
            'word_translates',
             Translate.objects.filter(language_code__in=[lang,user_lang]))

        if not value:
            return Response({'detail': 'value is required.'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = queryset.filter(value=value, language_code =lang).prefetch_related(defi_prefetch,trans_prefetch,form_prefetch)[:20]

        # if not queryset.exists():
        #     return Response({'detail': 'Word not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(queryset, many=True)
        ai_modify_data = test(value, serializer.data, lang, user_lang)
        return Response({"clgt":ai_modify_data})

    @action(detail=False, methods=['get'], url_path='get-word')
    def search_word(self, request, pk=None, *args, **kwargs):
        queryset = self.get_queryset()
        value = request.query_params.get('value')
        lang = request.query_params.get('lang')
        user_lang = request.query_params.get('user_lang')
    def get_queryset(self):
        # Nếu muốn lọc theo người dùng hoặc trạng thái
        queryset = super().get_queryset()
        user = self.request.user if self.request.user.is_authenticated else None

        # Ví dụ: chỉ lấy các Word đang hoạt động
        queryset = queryset.filter(is_active=True)

        # Nếu muốn lọc theo người tạo (nếu có trường user), bạn có thể thêm:
        # if user:
        #     queryset = queryset.filter(user=user)

        return queryset

import copy
def deep_merge(delta, base):
    """
    Merge delta vào base. 
    - Nếu cả hai đều là dict, sẽ merge đệ quy.
    - Ưu tiên giá trị từ delta (kể cả None).
    """
    if not delta: return base
    if not base: return delta
    # Nếu không phải cả hai đều là dict, lấy luôn delta (theo logic ưu tiên object1 của bạn)
    if not isinstance(delta, dict) or not isinstance(base, dict):
        return delta

    # Tạo bản copy từ base để không làm ảnh hưởng đến object cũ
    result = copy.deepcopy(base)

    for key, value in delta.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Nếu cả 2 đều là dict, đệ quy xuống tầng sâu hơn
            result[key] = deep_merge(value, result[key])
        else:
            # Nếu key mới hoàn toàn hoặc không phải dict, ghi đè/thêm mới từ delta
            result[key] = value

    return result