from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticatedOrReadOnly, AllowAny
from ai.models import AISense, AIWord, AISenseMetadata
from core.serializers import CollectionDetailSerializer
from core.models import Collection,CollectionItem, UserCollection
from core.serializers.UserCollection import UserCollectionSerializer
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from utils.utils.sense_handle import deep_merge
from utils.utils.uuidv7 import generate_uuid7
from django.db import transaction
from rest_framework.decorators import action
from django.contrib.auth.models import User
from django.forms.models import model_to_dict

class UserCollectionViewSet(SoftDeleteViewSet):
    queryset = UserCollection.objects.all()
    serializer_class = UserCollectionSerializer
    # permission_classes = [IsAuthenticatedOrReadOnly]
    permission_classes = [AllowAny]

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
        added_ids = list(user_col.added_item_ids or [])
        removed_ids = set(user_col.deleted_item_ids or [])

        # 2. Query lấy sạch hàng nền thuộc Collection + hàng tạm thời trong added_ids
        # Sử dụng select_related để HÚT luôn data của Ruột (AISense) trong đúng 1 câu SQL lệnh JOIN duy nhất
        raw_items = (
            CollectionItem.objects.filter(
                Q(collection_id=collection_id) | Q(id__in=added_ids)
            )
            .select_related('sense') # Tên field trỏ sang AISense của Sơn
            # .only('id', 'collection_id', 'value', 'sense_items__id', 'sense_items__word', 'sense_items__delta') # Tối ưu chỉ bốc các trường cần thiết
        )

        # 3. LỌC TRÊN RAM: Loại bỏ các item nằm trong danh sách đã xóa bằng Python Set (Tốc độ O(1))
        # Đồng thời Sơn có thể sort lại mảng theo ý muốn tại đây nếu cần
        user_col.items = [
            item for item in raw_items 
            if item.id not in removed_ids
        ]
        return Response(self.get_serializer(user_col).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='update-sense')
    def update_sense(self, request, pk=None, *args, **kwargs):
        # đổi sense của cùng 1 word cùng sẽ nằm trong case này
        # user = request.user
        collection_item_id = request.data.get('collection_item_id')
        user_collection_id = request.data.get('user_collection_id')

        if not collection_item_id or not user_collection_id:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing collection_item_id or user_collection_id"})
        
        delta = request.data.get('delta', {})
        metadata = request.data.get('metadata', None)
        # user_language_code = request.data.get('user_lang')
        user = request.user if request.user.is_authenticated else User.objects.first()

        with transaction.atomic():
            user_collection = UserCollection.objects.select_for_update().get(id=user_collection_id)
            collection_item = CollectionItem.objects.select_related('sense', 'sense__metadata').get(id=collection_item_id)
            sense = collection_item.sense
            metadata_instance = sense.metadata

            return_item_id = collection_item_id

            if not sense or not user_collection or not collection_item:
                return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "item not found"})

            is_frozen = sense.is_frozen

            image = request.data.get('image', sense.image_preview)


            if metadata:
                # Chuyển thành dict và xử lý
                new_data = model_to_dict(
                    sense.metadata,
                    exclude=[
                        'id',
                        'created_at',
                        'updated_at',
                        'created_by',
                        'updated_by'
                    ]
                )

                # Cập nhật các trường muốn thay đổi
                new_data.update({
                    **metadata
                })

                # Tạo instance mới
                metadata_instance = AISenseMetadata.objects.create(**new_data)


            if is_frozen:
                new_sense_id = generate_uuid7()
                new_collection_item_id = generate_uuid7()

                return_item_id = new_collection_item_id

                # Chuyển thành dict và xử lý
                new_data = model_to_dict(
                    sense.metadata,
                    exclude=[
                        'id',
                        'created_at',
                        'updated_at',
                        'created_by',
                        'updated_by',
                        "content"
                    ]
                )

                # Cập nhật các trường muốn thay đổi
                new_data.update({
                    "delta":delta, 
                    "image_preview":image,
                    "metadata" : metadata_instance,
                    "created_by":user, 
                    "is_official":False,
                    "is_frozen" :False,
                    "is_ai_created":False
                })

                # Tạo instance mới
                new_sense = AISense.objects.create(**new_data)

                if sense.is_official:
                    new_sense.original = sense.original or sense

                new_collection_item = CollectionItem(id=new_collection_item_id,sense_id=new_sense_id, original_id=collection_item.original_id, value=collection_item.value, order = collection_item.order)

                # Kiểm tra collection item cũ có nằm trong danh sách added_item_ids không
                in_added_ids = collection_item.id in (user_collection.added_item_ids or [])

                added_set = set(user_collection.added_item_ids or [])
                deleted_set = set(user_collection.deleted_item_ids or [])

                if in_added_ids:
                    # Thay thế vị trí của collection item cũ trong added
                    added_set.remove(collection_item.id)
                    added_set.add(new_collection_item_id)
                    
                else:
                    # Thêm new collection item vào added_item_ids
                    added_set.add(new_collection_item_id)
                    # Thêm collection item cũ vào deleted_item_ids?
                    deleted_set.add(collection_item.id)

                user_collection.added_item_ids = list(added_set)
                user_collection.deleted_item_ids = list(deleted_set)

                new_sense.save()
                new_collection_item.save()
                user_collection.save()

            else:
                # Trực tiếp sửa sense
                sense.delta = delta
                sense.image_preview = image
                sense.metadata = metadata_instance
                sense.save()

        return Response(status=status.HTTP_200_OK, data={"message": "Success", "collection_item_id": return_item_id})
        
    @action(detail=True, methods=['post'], url_path='add-sense')
    def add_sense(self, request, pk=None, *args, **kwargs):
        # Có 2 case: add new tự gõ hoặc thêm 1 sense có sẵn
        # case tự gõ thì sẽ trigger 1 hàm create sense trước, sau đó mới gọi hàm này với id vừa tạo
        # => Luôn có sense_id, check đảm bảo sense này chưa tồn tại trong collection và added
        # Tạo collection_item và sau đó thêm vào added_item_ids
        # Kiểm tra từ này đã học chưa. Nếu học rồi thì thêm learn + 1

        collection_item_id = request.data.get('collection_item_id')
        user_collection_id = request.data.get('user_collection_id')

        if not collection_item_id or not user_collection_id:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing collection_item_id or user_collection_id"})

        user = request.user if request.user.is_authenticated else User.objects.first()

        with transaction.atomic():
            user_collection = UserCollection.objects.select_for_update().get(id=user_collection_id)
            existing = CollectionItem.objects.filter(id=collection_item_id, collection_id=user_collection.collection_id).count()

            added_set = set(user_collection.added_item_ids or [])

            if existing > 0 or collection_item_id in added_set:
                return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Item already exists"})

            collection_item = CollectionItem.objects.select_for_update().get(id=collection_item_id)

            added_set.add(collection_item_id)

            user_collection.added_item_ids = list(added_set)
            user_collection.save()

            # Kiểm tra từ này đã học chưa. Nếu đã học thì trừ số từ đã học đi 1
            # pending

        return Response(status=status.HTTP_200_OK, data={"message": "Success", "item_id": collection_item_id})

    @action(detail=True, methods=['post'], url_path='delete-sense')
    def delete_sense(self, request, pk=None, *args, **kwargs):
        collection_item_id = request.data.get('collection_item_id')
        user_collection_id = request.data.get('user_collection_id')

        if not collection_item_id or not user_collection_id:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing collection_item_id or user_collection_id"})

        user = request.user if request.user.is_authenticated else User.objects.first()

        with transaction.atomic():
            user_collection = UserCollection.objects.select_for_update().get(id=user_collection_id)
            collection_item = CollectionItem.objects.select_related('sense_items', 'sense_items__metadata').get(id=collection_item_id)

            # Kiểm tra từ này đã học chưa. Nếu đã học thì trừ số từ đã học đi 1
            # pending

            if not user_collection or not collection_item:
                return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "item not found"})

            deleted_set = set(user_collection.deleted_item_ids or [])

            deleted_set.add(collection_item_id)

            user_collection.deleted_item_ids = list(deleted_set)
            
            user_collection.save()

        return Response(status=status.HTTP_200_OK, data={"message": "Success", "item_id": collection_item_id})

    
    @action(detail=True, methods=['post'], url_path='clear-custom')
    def clear_custom(self, request, pk=None, *args, **kwargs):
        user_collection_id = request.data.get('user_collection_id')

        if not user_collection_id:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing user_collection_id"})

        user = request.user if request.user.is_authenticated else User.objects.first()

        with transaction.atomic():
            user_collection = UserCollection.objects.select_for_update().get(id=user_collection_id)

            if not user_collection:
                return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "item not found"})

            user_collection.deleted_item_ids = []
            user_collection.added_item_ids = []
            user_collection.save()

        return Response(status=status.HTTP_200_OK, data={"message": "Success", "user_collection_id": user_collection_id})
    
    @action(detail=True, methods=['post'], url_path='upload')
    def upload(self, request, pk=None, *args, **kwargs):
        user_collection_id = request.data.get('user_collection_id')

        if not user_collection_id:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing user_collection_id"})

        user = request.user if request.user.is_authenticated else User.objects.first()

        with transaction.atomic():
            # 1. Khóa dòng UserCollection để đóng gói dữ liệu an toàn
            user_collection = get_object_or_404(UserCollection.objects.select_for_update(), id=user_collection_id)
            current_collection = user_collection.collection

            if not current_collection:
                return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "Current collection not found"})

            # Bóc tách mảng ra RAM
            added_ids = list(user_collection.added_item_ids or [])
            deleted_ids = set(user_collection.deleted_item_ids or [])

            # 2. Khởi tạo một thực thể Collection mới hoàn toàn để đóng gói phiên bản độc lập
            current_parent_id = current_collection.parent_id or []
            current_parent_id.append(current_collection.id)
            new_collection_id = generate_uuid7()
            new_collection = Collection.objects.create(
                id=new_collection_id,
                title=f"{current_collection.title} (v{int(getattr(current_collection, 'version', 1)) + 1})",
                description=current_collection.description,
                created_by=user,
                original_id=current_collection.original_id or current_collection.id,
                parent = current_parent_id,
                image=current_collection.image,
                image_url=current_collection.image_url,
                items_count = current_collection.items_count + len(added_ids) - len(deleted_ids)

            )

            # 3. PHÂN TÁCH XỬ LÝ 2 NHÁNH DỮ LIỆU ĐỂ TỐI ƯU CỰC ĐỈNH:
            
            # --- Nhánh A: Hàng nền hệ thống cũ (Chỉ lấy những từ KHÔNG nằm trong danh sách xóa) ---
            base_items = CollectionItem.objects.filter(collection_id=current_collection.id)
            
            items_to_duplicate = []
            for item in base_items:
                if item.id in deleted_ids:
                    continue  # User đã xóa từ này -> Bỏ qua không mang sang bộ mới
                
                # Tạo bản sao vỏ mới trỏ sang bến đỗ mới
                items_to_duplicate.append(
                    CollectionItem(
                        id=generate_uuid7(),
                        collection_id=new_collection_id,
                        sense_id=item.sense_id, # Dùng chung ruột
                        original_id=item.original_id or item.id,
                        value=item.value
                    )
                )
            
            # Khởi nện SQL bulk_create tạo hàng loạt bản sao vỏ
            if items_to_duplicate:
                CollectionItem.objects.bulk_create(items_to_duplicate)

            # --- Nhánh B: Hàng tự tạo lưu tạm (Đang nằm trong added_item_ids) ---
            # Lọc lại danh sách hàng tạm thực sự được giữ lại (phòng hờ user vừa add xong lại bấm xóa luôn)
            valid_added_ids = [item_id for item_id in added_ids if item_id not in deleted_ids]
            
            if valid_added_ids:
                # 🎯 Cập nhật trực tiếp: Đóng dấu gắn kết vào Collection mới cho các item tạm thời này
                CollectionItem.objects.filter(id__in=valid_added_ids).update(
                    collection_id=new_collection_id
                )

            # 4. GỘT RỬA TRẠNG THÁI: Reset mảng phẳng về rỗng, trỏ UserCollection sang bến đỗ mới
            user_collection.collection_id = new_collection_id
            user_collection.added_item_ids = []
            user_collection.deleted_item_ids = []
            user_collection.save()

        return Response(
            status=status.HTTP_200_OK, 
            data={
                "message": "Upload and official versioning success", 
                "new_collection_id": new_collection_id,
                "user_collection_id": user_collection.id
            }
        )

    def perform_create(self, serializer):
        # Nếu bạn muốn gán user tạo collection
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()
