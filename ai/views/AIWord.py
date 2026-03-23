from datetime import datetime
from operator import ne
from urllib.parse import unquote
import asyncio
from importlib import metadata
import threading
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from ai.models.AISense import AISense
from ai.models.AISenseContent import AISenseContent
from ai.models.TranslateLog import TranslateLog
from core.models import Defination, Example, ExampleTranslate, Translate, WordForm
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
from utils.utils.sense_handle import serialize_entries, serialize_senses
from utils.utils.socket import get_safe_room_id, run_async_task, socket_close
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from django.contrib.auth.models import User

def detect_missing_content(senses, contents, language_code, user_language_code):
    # Map để lấy value và type nhanh từ danh sách content instance
    c_map = {str(c.id): c for c in contents}
    
    missing_list = {}
    need_translation = {}
    sense_def_ids = {} # Lưu ID của definition để tra cứu sau

    for s in senses:
        sid = str(s.id)
        l = []
        c_json = s.contents or {}

        if not c_json.get('translations',{}).get(user_language_code):
            need_translation[sid] = True
        
        for c_type, j in c_json.items():
            # 1. Kiểm tra bản dịch tổng quát của cả Sense
            if c_type == 'translations':
                continue
            
            # 2. Xử lý các node Dict (definition, usage)
            elif isinstance(j, dict):
                orig_id = j.get(language_code)
                # Lưu lại ID định nghĩa gốc để dùng cho bước sau
                if c_type == 'definition':
                    sense_def_ids[sid] = orig_id
                
                # Nếu có gốc mà thiếu đích
                if orig_id and not j.get(user_language_code):
                    c_obj = c_map.get(str(orig_id))
                    if c_obj:
                        l.append({'id': str(orig_id), 'type': c_type, 'value': c_obj.value})
            
            # 3. Xử lý mảng (examples)
            elif isinstance(j, list):
                for ex in j:
                    ex_orig_id = ex.get(language_code)
                    if ex_orig_id and not ex.get(user_language_code):
                        c_obj = c_map.get(str(ex_orig_id))
                        if c_obj:
                            l.append({'id': str(ex_orig_id), 'type': 'example', 'value': c_obj.value})
        
        if l:
            missing_list[sid] = l

    # 4. Bước cuối: Nếu Sense cần dịch, bốc giá trị của Definition gốc bỏ vào
    for sid in list(need_translation.keys()):
        def_id = sense_def_ids.get(sid)
        if def_id:
            c_obj = c_map.get(str(def_id))
            need_translation[sid] = c_obj.value if c_obj else True
        else:
            # Nếu ko có definition (trường hợp hy hữu), xóa khỏi list cần dịch hoặc để True
            del need_translation[sid]

    return [missing_list, need_translation]

class AIWordViewSet(SoftDeleteViewSet):
    queryset = AIWord.objects.all()
    serializer_class = AIWordSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['get'], url_path='get-word')
    def get_word(self, request, pk=None, *args, **kwargs):
        start_time = datetime.now()

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
            word_instance = AIWord.objects.create(
            value=word,
            language_code=language_code,
            created_by=user,
            status='PROCESSING'
            )

            print(12)
            # GỌI CELERY: Bọc phát mất hút ở đây
            ai_create_new_word_task.delay(
                user.id, 
                word_instance.id, 
                language_code, 
                user_language_code, 
                socket_room
            )
            # ai_create_new_word(user.id, word_instance.id, language_code, user_language_code, socket_room)
            print(2)
            return Response({'detail': 'PROCESSING', 'status': '202'}, status=status.HTTP_202_ACCEPTED)
           

        # Word is processing, return not found. send data via socket when finish
        if word_instance.status == 'PROCESSING':
            # Keep connect with socket to get result
            return Response({'detail': 'PROCESSING', 'status': '202'}, status=status.HTTP_202_ACCEPTED)
        
        if word_instance.status == 'REJECTED':
            # Keep connect with socket to get result
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
