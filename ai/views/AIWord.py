from datetime import datetime
from operator import ne
from urllib.parse import unquote
import asyncio
from importlib import metadata
import threading
from asgiref.sync import async_to_sync, sync_to_async
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticatedOrReadOnly, AllowAny
from ai.models.AISense import AISense
from ai.models.AISenseContent import AISenseContent
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.TranslateLog import TranslateLog
from core.models import Defination, Example, ExampleTranslate, Translate, WordForm, ImageLibrary, ImageContext, ImageLibraryContext
from ai.models.AIWord import AIWord
from django.db.models import Prefetch, Window, F, Q
from django.db.models.functions import RowNumber
from ai.serializers.AIWord import AIWordSerializer
from core.models.Language import Language
from utils.celery.word import ai_create_new_word_task
from utils.utils import uuidv7
from utils.utils.ai import ai_create_new_word, render_translate
from utils.utils.background_task import background_task
from utils.utils.flatten_id import flatten_ids_by_langs
from utils.utils.limit_prefetch import limit_prefetch
from utils.utils.sense_handle import serialize_entries, serialize_senses, get_user_lang_content, get_word_lang_content
from utils.utils.socket import get_safe_room_id, run_async_task, socket_close
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from django.contrib.auth.models import User
from utils.redis.word_init import WordCacheManager
from utils.ai.translate import ai_create_translate_sema

def detect_missing_content(senses, language_code, user_language_code):
    # Kết quả trả về: { "sense_id": { "definition": "...", "examples": [...] } }
    missing_data = []
    # Danh sách các Sense cần dịch nghĩa tổng quát (translations field)

    for s in senses:
        sid = str(s.id)
        c_json = s.contents or {}
        sense_missing = {}

        # --- 1. Kiểm tra Bản dịch tổng quát (field: translations) ---
        trans_node = c_json.get('translations', {})
        if not trans_node.get(user_language_code):
            # Nếu thiếu bản dịch tổng quát, lấy definition làm gốc để AI dịch
            def_node = c_json.get('definition', {})
            sense_missing['translations'] = def_node.get(language_code, True)
        else :
            sense_missing['translations'] = False

        # --- 2. Duyệt qua các thành phần bên trong contents ---
        for c_type, j in c_json.items():
            if c_type in ['translations', 'collocations', 'idioms', 'metadata']:
                continue
            
            # Xử lý Dictionary (definition, usage, pronunciation...)
            if isinstance(j, dict):
                orig_val = j.get(language_code)
                if orig_val and not j.get(user_language_code):
                    sense_missing.setdefault(c_type,{})[language_code] = orig_val
            
            # Xử lý List (examples)
            elif isinstance(j, list) and c_type == 'examples':
                ex_missing = []
                for index, ex in enumerate(j):
                    ex_orig = ex.get(language_code)
                    if ex_orig and not ex.get(user_language_code):
                        ex_missing.append({'index': index, language_code:{'value': ex_orig}})
                
                if ex_missing:
                    sense_missing['examples'] = ex_missing

        # Nếu chỉ có Translate = False thì là ko cần dịch
        if sense_missing['translations'] ==False and len(list(sense_missing.keys()))>1:
            missing_data.append({"id": str(s.id),"contents":{**sense_missing}})

    return missing_data

class AIWordViewSet(SoftDeleteViewSet):
    queryset = AIWord.objects.all()
    serializer_class = AIWordSerializer
    # permission_classes = [IsAuthenticatedOrReadOnly]
    permission_classes = [AllowAny]

    @action(detail=False, methods=['get'], url_path='get-word')
    def get_word(self, request, pk=None, *args, **kwargs):
        start_time = datetime.now()
        cache_manager = WordCacheManager()


        # params: value, lang, user_lang
        # client create socket connect with word(md5)+language+user_language
        # user = User.objects.first()  # Thay thế bằng cách lấy user thực tế
        user = User.objects.get_or_create(username='admin', password='admin', email='admin@gmail.com', is_superuser=True)[0]
        raw_word = request.GET.get('value')
        word = unquote(raw_word).strip().lower()
        language_code = request.GET.get('lang')
        user_language_code = request.GET.get('user_lang')
        socket_room = get_safe_room_id(word, language_code, user_language_code) 
        socket_room = 'test'

        # Lấy dữ liệu trong redis
        cached = cache_manager.cache_word_get_data(language_code, word)

        if cached:
            # Nếu full thì trực tiếp lấy dữ liệu và return
            if cached.get('status') == 'CACHED':
                cached_word = cached.get('word')
                # languages = cached.get('languages')

                # if user_language_code not in languages:
                #     word_lang_content, missing_contents = get_word_lang_content(language_code, cached_word)
                #     asyncio.create_task(ai_create_translate_sema({
                #         "word_value": word,
                #         "language_code": language_code,
                #         "user_language_code": user_language_code,
                #         "sense_info": missing_contents
                #     }, False))
                #     # return language_code val, run translate and send from socket
                #     return Response({'detail': 'CACHED, NEW TRANSLATED', 'status': '201', 'data':word_lang_content}, status=status.HTTP_201_CREATED)
                
                user_lang_entries, missing_contents, current_senses = get_user_lang_content(language_code, user_language_code, cached_word)

                if missing_contents:
                    asyncio.create_task(ai_create_translate_sema({
                        "word_value": word,
                        "language_code": language_code,
                        "user_language_code": user_language_code,
                        "missing_translate": missing_contents,
                        'current_senses':current_senses
                    }, False))
                    return Response({'detail': 'CACHED', 'status': '200', "data":{**cached_word, "entries": user_lang_entries}}, status=status.HTTP_206_PARTIAL_CONTENT)


                return Response({'detail': 'CACHED', 'status': '200', "data":{**cached_word, "entries": user_lang_entries}}, status=status.HTTP_200_OK)
                # return language_code val, run translate and send from socket
                return Response({'detail': 'CACHED, NEW TRANSLATED', 'status': '201', 'data':word_lang_content}, status=status.HTTP_201_CREATED)
            # Nếu có dữ liệu nhưng không full thì duy trì socket và trả về init
            elif cached.get('status') == 'PROCESSING':
                return Response({'detail': 'PROCESSING', 'status': '202', 'data':cached}, status=status.HTTP_202_ACCEPTED)
            
            elif cached.get('status') == 'ERROR':
                return Response({'detail': 'ERROR', 'status': '500', 'data':cached}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            elif cached.get('status') == 'NOT_FOUND':
                return Response({'detail': 'NOT_FOUND', 'status': '404', 'data':cached}, status=status.HTTP_404_NOT_FOUND)
            
        else:
            # Nếu chưa có dữ liệu thì lấy lấy dữ liệu từ db
            sense_filter = Q(is_official=True)
            # sense_filter = Q(is_official=True) | Q(created_by=user) | Q(updated_by=user)

            # 2. Prefetch Logs - Chỉ lấy cái mới nhất đang PROCESSING
            # Thay vì Window function, ta dùng order_by thông thường
            log_qs = TranslateLog.objects.filter(
                language_code=user_language_code, 
                status="PROCESSING"
            ).order_by('-created_at')

            print(1)

            # 3. Prefetch Senses
            sense_qs = AISense.objects.filter(is_official=True).select_related('metadata').order_by('is_official')

            # 4. Gộp vào query chính
            word_instance = AIWord.objects.filter(value=word, language_code=language_code).prefetch_related(
                Prefetch('senses', queryset=sense_qs, to_attr='prefetched_senses')
            ).first()

            # Nếu từ này chưa được khởi tạo ở db
            if not word_instance or word_instance.status == 'FAILED':
                created, init_data = cache_manager.cache_word_init(language_code, word, user_language_code)

                # Nếu có người khác khởi tạo trước, trả về và chờ
                if not created:
                    print("Word is processing")
                    cache_manager.cache_word_add_translate(language_code, word, user_language_code)
                    return Response({'detail': 'PROCESSING', 'status': '202', 'data':init_data}, status=status.HTTP_202_ACCEPTED)

                word_instance = AIWord.objects.create(
                **init_data.get('word'),
                created_by=user,
                status= "PROCESSING"
                )

                print(12)
                import json
                import redis
                
                # Kết nối tới DB 0 (Làn đường xử lý)
                r_queue = redis.Redis(host='redis', port=6379, db=0)

                # Đẩy vào queue "redis_word"
                r_queue.rpush("redis_word", json.dumps({
                    "user_id": user.id,
                    "word_id": word_instance.id,
                    "value": word,
                    "language_code": language_code,
                    "user_language_code": user_language_code
                }))
                # r_queue.rpush("redis_word", json.dumps(raw_word))
                
                print(f"[QUEUE] Pushed word '{word}' to redis_word queue")
                # ---------------------------------------------
                # GỌI CELERY: Bọc phát mất hút ở đây
                # ai_create_new_word_task.delay(
                #     user.id, 
                #     word_instance.id, 
                #     language_code, 
                #     user_language_code, 
                #     socket_room
                # )
                # ai_create_new_word(user.id, word_instance.id, language_code, user_language_code, socket_room)
                print(2)
                return Response({'detail': 'PROCESSING', 'status': '202', 'data': init_data}, status=status.HTTP_201_CREATED)
           

            # Đây là nếu từ đã tồn tại trong db
            else:
                # pass
                senses_instance = word_instance.prefetched_senses
                entries = serialize_entries(senses_instance)
                word_instance.processed_entries = entries

                data = AIWordSerializer(word_instance).data

                user_lang_entries, missing_contents, current_senses = get_user_lang_content(language_code, user_language_code, data)

                # Content missing. Return current, generate new and send via socket
                if missing_contents:
                    # Unique (word, user_language_code with 1 status PROCESSING allowed)
                    try:
                        from utils.celery.translate import task_create_translate
                        task_create_translate.delay({
                            "word_value": word,
                            "language_code": language_code,
                            "user_language_code": user_language_code,
                            "missing_translate": missing_contents,
                            'current_senses':current_senses
                        }, False)
                        # translate_instance = TranslateLog.objects.create(word=word_instance, language_code=user_language_code, status="PROCESSING")
                        # background_task(render_translate(user, translate_instance, word, senses_instance, missing_contents , need_translation, language_code, user_language_code, socket_room))
                    except:
                        pass

                    # Keep connect with socket to get result
                    return Response({'detail': 'Word incomplete.', 'status': '206', 'data': {**data, "entries": user_lang_entries}}, status=status.HTTP_206_PARTIAL_CONTENT)

                cache_manager.cache_word(language_code, word, data)

                # Word ok, return data, close socket
                return Response({'detail':"DATABASE_DATA","data":{**data, "entries": user_lang_entries}}, status=status.HTTP_200_OK)

                # Get original senses
                # Tiến hành merge, kiểm tra đủ bản dịch hay chưa, delay translate
                # Lưu vào redis
                # Trả dữ liệu về 
                


            # 5. Sử dụng sau đó (Cực nhanh vì là list Python)
            if word_instance:
                translating = word_instance.active_logs[0] if word_instance.active_logs else None



            

        if not created:
                print("Word is processing")
                cache_manager.cache_word_add_translate(language_code, word, user_language_code)
                return Response({'detail': 'PROCESSING', 'status': '202', 'data':init_data}, status=status.HTTP_202_ACCEPTED)
        # Nếu có thì lưu vào redis và trả về full, detect thiếu bản dịch thì duy trì socket, delay dịch
        # Nếu chưa có thì tạo bản ghi mới trong redis, nếu sucess thì tạo bản ghi vào word, delay tạo
        # #


        print(1)

        # 1. Định nghĩa bộ lọc Sense
        sense_filter = Q(is_official=True) | Q(created_by=user) | Q(updated_by=user)

        # 2. Prefetch Logs - Chỉ lấy cái mới nhất đang PROCESSING
        # Thay vì Window function, ta dùng order_by thông thường
        log_qs = TranslateLog.objects.filter(
            language_code=user_language_code, 
            status="PROCESSING"
        ).order_by('-created_at')

        print(1)

        # 3. Prefetch Senses
        sense_qs = AISense.objects.filter(sense_filter).select_related('metadata', 'previous').order_by('-updated_at')

        # 4. Gộp vào query chính
        word_instance = AIWord.objects.filter(value=word, language_code=language_code).prefetch_related(
            Prefetch('translate_logs', queryset=log_qs, to_attr='active_logs'),
            Prefetch('senses', queryset=sense_qs, to_attr='prefetched_senses')
        ).first()

        # 5. Sử dụng sau đó (Cực nhanh vì là list Python)
        if word_instance:
            senses_instance = word_instance.prefetched_senses # Thay vì .senses.all()
            translating = word_instance.active_logs[0] if word_instance.active_logs else None

        # Word not found: Generate new word, return not found. send data via socket when finish
        if not word_instance or word_instance.status == 'FAILED':

            created, init_data = cache_manager.cache_word_init(language_code, word, user_language_code)

            if init_data.get('status') == 'REDIS-CACHED':
                return Response({'detail': 'REDIS-CACHED', 'status': '200', 'data':init_data}, status=status.HTTP_200_OK)

            if not created:
                print("Word is processing")
                cache_manager.cache_word_add_translate(language_code, word, user_language_code)
                return Response({'detail': 'PROCESSING', 'status': '202', 'data':init_data}, status=status.HTTP_202_ACCEPTED)

            word_instance = AIWord.objects.create(
            **init_data.get('word'),
            created_by=user,
            status= "PROCESSING"
            )

            print(12)
            # GỌI CELERY: Bọc phát mất hút ở đây
            # sửa lại, bỏ content, lưu toàn bộ ở bảng sense, dùng cơ chế origin-delta
            ai_create_new_word_task.delay(
                user.id, 
                word_instance.id, 
                language_code, 
                user_language_code, 
                socket_room
            )
            # ai_create_new_word(user.id, word_instance.id, language_code, user_language_code, socket_room)
            print(2)
            return Response({'detail': 'PROCESSING', 'status': '202', 'data': init_data}, status=status.HTTP_202_ACCEPTED)
           

        # Word is processing, return not found. send data via socket when finish
        if word_instance.status == 'PROCESSING':
            # Keep connect with socket to get result
            return Response({'detail': 'PROCESSING', 'status': '202'}, status=status.HTTP_202_ACCEPTED)
        
        if word_instance.status == 'REJECTED':
            # kill socket connect
            return Response({'detail': 'Rejected', 'status': '400'}, status=status.HTTP_400_BAD_REQUEST)

        target_langs = [language_code, user_language_code]

        content_ids = set()
        for sense in senses_instance:
            if sense.contents:
                # Chỉ nhặt ID của en và vi, bỏ qua tất cả các ngôn ngữ khác trong JSON
                content_ids.update(flatten_ids_by_langs(sense.contents, target_langs))


        translating = word_instance.translate_logs.first()

        contents = AISenseContent.objects.filter(id__in=content_ids)
        senses = serialize_senses(senses_instance, contents, language_code, user_language_code)
        entries = serialize_entries(senses)
        word_instance.processed_entries = entries


        data = AIWordSerializer(word_instance).data

        if translating and translating.status == 'PROCESSING':
            return Response({"data":data,'detail': 'Word incomplete.', 'status': '206', 'data': data}, status=status.HTTP_206_PARTIAL_CONTENT)

        else:
            [missing_contents,need_translation] = detect_missing_content(senses_instance, contents, language_code, user_language_code)


            # Content missing. Return current, generate new and send via socket
            if missing_contents or need_translation:
                # Unique (word, user_language_code with 1 status PROCESSING allowed)
                try:
                    translate_instance = TranslateLog.objects.create(word=word_instance, language_code=user_language_code, status="PROCESSING")
                    background_task(render_translate(user, translate_instance, word, senses_instance, missing_contents , need_translation, language_code, user_language_code, socket_room))
                except:
                    pass

                # Keep connect with socket to get result
                return Response({"data":data,'detail': 'Word incomplete.', 'status': '206', 'data': data, 'missing_contents': missing_contents}, status=status.HTTP_206_PARTIAL_CONTENT)

            # Word ok, return data, close socket
            return Response({"data":data}, status=status.HTTP_200_OK)
            # client close socket

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

    @action(detail=False, methods=['delete'], url_path='clear')
    def clear(self, request):
        try:
            # Xoá toàn bộ dữ liệu trên các bảng chỉ định
            # Lưu ý: .all().delete() sẽ kích hoạt các tín hiệu (signals)
            # Nếu muốn xoá cực nhanh và không cần signals, dùng ._raw_delete()
            cache = WordCacheManager()
            AISenseMetadata.objects.all().delete()
            ImageLibraryContext.objects.all().delete()
            ImageLibrary.objects.all().delete()
            ImageContext.objects.all().delete()
            AISenseContent.objects.all().delete()
            AISense.objects.all().delete()
            AIWord.objects.all().delete()
            TranslateLog.objects.all().delete()

            cache.cache_word_clear_all()

            return Response(
                {"message": "Đã xoá sạch dữ liệu trên các bảng được chỉ định!"}, 
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {"error": str(e)}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

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
