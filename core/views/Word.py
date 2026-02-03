from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from core.models import Defination, Example, ExampleTranslate, Translate, WordForm
from core.models.Word import Word
from django.db.models import Prefetch, Window, F
from django.db.models.functions import RowNumber
from core.serializers.Word import WordSerializer
# from utils.utils.ai import test
from utils.utils.limit_prefetch import limit_prefetch
from utils.utils.soft_delete_viewset import SoftDeleteViewSet

class WordViewSet(SoftDeleteViewSet):
    queryset = Word.objects.all()
    serializer_class = WordSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['get'], url_path='get-word')
    def get_word(self, request, pk=None, *args, **kwargs):
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

        if not queryset.exists():
            return Response({'detail': 'Word not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(queryset, many=True)
        # ai_modify_data = test(value, serializer.data, lang, user_lang)
        return Response({"data":serializer.data}, status=status.HTTP_200_OK)
    
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

        # print(queryset.query)
        return queryset
