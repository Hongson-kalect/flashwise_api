from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from ai.models.AISense import AISense
from core.models.Collection import Collection
from core.models.CollectionItem import CollectionItem
from core.models.UserCollection import UserCollection
from core.serializers.Collection import CollectionSerializer
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from django.db import transaction
from rest_framework.decorators import action

class UserCollectionViewSet(SoftDeleteViewSet):
    queryset = UserCollection.objects.all()
    serializer_class = CollectionSerializer
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
    
    @action(detail=True, methods=['post'], url_path='get-collection')
    def get_user_collection_detail(self, request, pk=None):
        user = request.user
        user_col_sub_id = request.params.get('user_collection_id')

        # 1. Đảm bảo lấy đúng UserCollection của user đó
        # Dùng prefetch_related để kéo luôn các Sense liên quan qua bảng trung gian
        # Sắp xếp luôn theo thứ tự gốc (order) của Collection
        
        user_col = get_object_or_404(
            UserCollection.objects.select_related('collection').prefetch_related(
                Prefetch(
                    'collection__senses',
                    queryset=AISense.objects.all().order_by('collectionitem__order'),
                    to_attr='original_senses'
                )
            ),
            sub_id=user_col_sub_id,
            user=user
        )

        # 2. Lấy các ID từ JSONField
        added_ids = user_col.added_sense or []
        removed_ids = user_col.removed_sense or []

        # 3. Lấy dữ liệu chi tiết cho các "Added Senses" (vì trong JSON chỉ lưu ID)
        # Bước này cần 1 query phụ nếu added_ids có dữ liệu
        added_senses_dict = {}
        if added_ids:
            added_senses_qs = AISense.objects.filter(id__in=added_ids)
            # Chuyển về dict để map cho nhanh theo thứ tự của added_ids
            added_senses_dict = {str(s.id): s for s in added_senses_qs}

        # 4. Hợp nhất logic (Merge)
        final_list = []

        removed_set = set(removed_ids)
        
        # - Thêm những từ Gốc (loại bỏ những từ trong removed)
        if user_col.collection:
            for sense in user_col.collection.original_senses:
                if str(sense.id) not in removed_set:
                    final_list.append(sense)

        # - Thêm những từ Mới (Added) - để ở cuối hoặc đầu tùy bạn
        # Ở đây mình để ở cuối theo đúng thứ tự user đã add
        for s_id in added_ids:
            if s_id in added_senses_dict:
                final_list.append(added_senses_dict[s_id])

        return {
            "metadata": {
                "name": user_col.collection.name if user_col.collection else "Custom Collection",
                "description": user_col.collection.description if user_col.collection else "",
                "image": user_col.collection.image.url if user_col.collection and user_col.collection.image else None,
                "is_official": user_col.collection.is_official if user_col.collection else False,
                "sub_id": user_col.sub_id,
            },
            "added_ids": added_ids, # Trả về để FE biết cái nào là hàng "thêm ngoài"
            "senses": final_list # Danh sách đã được merge và giữ thứ tự
        }


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
