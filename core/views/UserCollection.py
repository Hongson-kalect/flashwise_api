from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from ai.models import AISense, AIWord
from core.serializers import CollectionDetailSerializer
from core.models import Collection,CollectionItem, UserCollection
from core.serializers.UserCollection import UserCollectionSerializer
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from django.db import transaction
from rest_framework.decorators import action

class UserCollectionViewSet(SoftDeleteViewSet):
    queryset = UserCollection.objects.all()
    serializer_class = UserCollectionSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    # Giả sử nằm trong CollectionViewSet
    @action(detail=True, methods=['post'], url_path='download')
    def download(self, request, pk=None):
        collection = self.get_object() # Lấy collection theo ID trên URL
        
        # Tạo hoặc cập nhật trạng thái sở hữu
        user_col, created = UserCollection.objects.get_or_create(
            user=request.user,
            source_collection=collection,
            defaults={'is_owner': False}
    )
        
        if not created:
            return Response({"detail": "Bạn đã sở hữu bộ sưu tập này rồi."}, status=200)

        return Response({"detail": "Tải bộ sưu tập thành công."}, status=201)
    
    # @action(detail=True, methods=['post'], url_path='get-collection')
    # def get_user_collection_detail(self, request, pk=None):
        
    # override create request
    def create(self, request, *args, **kwargs):
        # params: name, description, image, tags, language_code, senses

        data = request.data
        senses_data = data.pop('senses', []) # Giả sử nhận một list sense IDs
        data.created_by = request.user

        # Sử dụng atomic để đảm bảo tính toàn vẹn dữ liệu
        with transaction.atomic():
            # 1. Lưu Collection trước
            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            collection = serializer.save()

            # 2. Chuẩn bị dữ liệu cho bảng phụ (CollectionItem)
            items_to_create = []
            for idx, sense_id in enumerate(senses_data):
                items_to_create.append(
                    CollectionItem(
                        collection=collection,
                        sense_id=sense_id, # Truyền ID trực tiếp để tránh query thêm
                        order=idx # Gán order theo thứ tự trong list gửi lên
                    )
                )

            # 3. Dùng bulk_create để "bắn" toàn bộ item vào DB trong 1 câu Query
            if items_to_create:
                CollectionItem.objects.bulk_create(items_to_create)

            UserCollection.objects.create(
                user=request.user,
                collection=collection,
                created_by=request.user,
            )

            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
        
    def list(self, request, *args, **kwargs):

        # Cần thêm: count số từ đã học
        # Collection có count số lượng sense từ bảng trung gian để có Tiến độ học

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    
    def retrieve(self, request, *args, **kwargs):
        user = request.user
        user_collection_id = kwargs.get('pk')

        # 1. Đảm bảo lấy đúng UserCollection của user đó
        # Dùng prefetch_related để kéo luôn các Sense liên quan qua bảng trung gian
        # Sắp xếp luôn theo thứ tự gốc (order) của Collection

        # Query 1: Lấy thông tin userCollection trước (Chỉ tốn vài miligiây)
        user_col = UserCollection.objects.prefetch_related('collection').get(id=user_collection_id)

        # Bố sung các từ đã được render trong pending_words vào collection_item
        collection = user_col.collection
        pending_words = collection.pending_words
        if pending_words:
            bulk_item = []
            pending_set = set(pending_words)
            invalid_words = collection.invalid_words or []
            success_words = []

            words = AIWord.objects.filter(value__in=pending_words)

            if len(words):
                for word in words:
                # từ đang pending hoặc chưa khởi tạo được thì vẫn kệ nó
                    if word.status=='INVALID':
                        invalid_words.append(word.value)
                        pending_set.remove(word.value)
                    
                    if word.status =='COMPLETED':
                        success_words.append(word.value)
                        pending_set.remove(word.value)
                
                if success_words:
                    sense_instances = AISense.objects.filter(word_value__in=success_words).order_by('word_value','-score').distinct('word_value')
                    for sense in sense_instances:
                        bulk_item.append(CollectionItem(sense_id=sense.id, collection_id=collection.id, original_id=sense.original_id, value=sense.word_value))

                with transaction.atomic():
                    if bulk_item:
                        CollectionItem.objects.bulk_create(bulk_item)
                    collection.pending_words = list(pending_set)
                    collection.invalid_words = invalid_words
                    collection.save()

        # Trích xuất các list ID ra khỏi bản ghi
        collection_id = user_col.collection_id
        added_ids = user_col.added_item_ids or []     # Mảng ID từ thêm riêng
        removed_ids = user_col.deleted_item_ids or [] # Mảng ID từ xóa bớt

        # Query 2: Lấy toàn bộ items trúng index Khóa chính và Khóa ngoại
        user_col.senses = AISense.objects.filter(
            id__in=CollectionItem.objects.filter(
                Q(collection_id=collection_id) | Q(sense_id__in=added_ids)
            ).exclude(sense_id__in=removed_ids).values_list('sense_id', flat=True)
        )

        return Response(self.get_serializer(user_col).data, status=status.HTTP_200_OK)

    def perform_create(self, serializer):
        # Nếu bạn muốn gán user tạo collection
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()
