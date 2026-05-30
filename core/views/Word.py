from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from core.models import WordForm
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
