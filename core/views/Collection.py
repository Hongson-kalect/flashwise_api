from django.db.models import Prefetch
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from core.models.Collection import Collection
from core.models.CollectionItem import CollectionItem
from core.models.UserCollection import UserCollection
from core.serializers.Collection import CollectionSerializer
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from django.db import transaction

class CollectionViewSet(SoftDeleteViewSet):
    queryset = Collection.objects.all()
    serializer_class = CollectionSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

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
        
    def get_queryset(self):
        # Chỉ lấy những Collection mà User này sở hữu hoặc Official
        # Dùng prefetch_related để kéo luôn các Item và Sense trong 1-2 query
        return Collection.objects.filter(
            # usercollection__user=self.request.user,
            is_active=True
        ).prefetch_related(
            Prefetch(
                'collectionitem_set', 
                queryset=CollectionItem.objects.select_related('sense').order_by('order')
            )
        ).distinct()

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        
        # Nếu muốn trả về format tùy chỉnh kèm danh sách senses
        data = serializer.data
        items = instance.collectionitem_set.all()
        
        # Format lại danh sách sense để client dễ dùng
        data['senses'] = [
            {
                "id": item.sense.id,
                "order": item.order,
                "content": item.sense.content # Giả sử Sense có field content
            } for item in items
        ]
        
        return Response(data)

    # def get_queryset(self):
    #     # Nếu muốn lọc theo người dùng hoặc trạng thái
    #     queryset = super().get_queryset()
    #     user = self.request.user if self.request.user.is_authenticated else None

    #     # Ví dụ: chỉ lấy các collection đang hoạt động
    #     queryset = queryset.filter(is_active=True)

    #     # Nếu muốn lọc theo người tạo (nếu có trường user), bạn có thể thêm:
    #     # if user:
    #     #     queryset = queryset.filter(user=user)

    #     return queryset

    def perform_create(self, serializer):
        # Nếu bạn muốn gán user tạo collection
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()
