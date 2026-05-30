from django.utils import timezone
from django.db.models import Prefetch, Q, Max
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
from django.db.models import Prefetch, Case, When, F, Value, Func, ExpressionWrapper, IntegerField

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
            for idx, sense_obj in enumerate(senses_data):
                if not sense_obj["sense_id"] or not sense_obj["original_id"]:
                    continue

                items_to_create.append(
                    CollectionItem(
                        collection=collection,
                        sense_id=sense_obj["sense_id"], # Truyền ID trực tiếp để tránh query thêm
                        original_id = sense_obj["original_id"], 
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
        current_user = request.user
        user_collection_id = kwargs.get('pk')

        # 1. Lấy thông tin UserCollection cùng với bộ sưu tập gốc
        user_col = get_object_or_404(
            UserCollection.objects.select_related('collection'), 
            id=user_collection_id
        )
        collection = user_col.collection

        if not collection:
            return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "Collection not found"})

        # === PHẦN 3: QUERY LẤY DỮ LIỆU THEO CẤU TRÚC MỚI (CHỐNG LẪN DATA) ===
        # Lấy các item đã phát hành (v thuộc về quá khứ của User) HOẶC hàng nháp của chính user hiện tại
        raw_items = CollectionItem.objects.filter(
            collection_id=collection.id
        ).filter(
            Q(released=True, version__lte=user_col.version) |
            Q(released=False, created_by=current_user)
        ).order_by(
            "original_id",
            "-released",   # Ưu tiên đưa hàng nháp (False) lên trước hàng đã release (True)
            "-version",    # Lấy version mới nhất đè lên version cũ
            "-created_at"  # Sắp xếp theo thời gian tạo mới nhất nếu trùng version
        ).distinct("original_id").select_related('sense')

        # Loại bỏ các item mang cờ xóa (Tombstone)
        # Sau đó sắp xếp lại danh sách cuối cùng theo trục 'order' (float) để hiển thị lên UI cho đúng thứ tự kéo thả
        final_items = sorted(
            [item for item in raw_items if not item.is_deleted],
            key=lambda x: x.order
        )

        # Gán danh sách item sạch sẽ vào thuộc tính tạm để Serializer đóng gói trả về cho Client
        user_col.items = final_items

        return Response(self.get_serializer(user_col).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='update-sense')
    def update_sense(self, request, *args, **kwargs):
        collection_item_id = request.data.get('collection_item_id')
        user_collection_id = request.data.get('user_collection_id')

        if not collection_item_id or not user_collection_id:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing IDs"})
        
        delta = request.data.get('delta', {})
        metadata = request.data.get('metadata', None)
        image = request.data.get('image')
        current_user = request.user if request.user.is_authenticated else None

        with transaction.atomic():
            user_collection = get_object_or_404(UserCollection.objects.select_for_update(), id=user_collection_id)
            
            # Kéo luôn cả tầng sense cũ ra để chuẩn bị nhân bản
            current_item = get_object_or_404(
                CollectionItem.objects.select_related('sense', 'sense__metadata').filter(collection_id=user_collection.collection_id),
                id=collection_item_id
            )
            old_sense = current_item.sense

            # Xử lý metadata mới nếu có
            updated_metadata = old_sense.metadata_dict_or_json if hasattr(old_sense, 'metadata_dict_or_json') else {}
            if metadata:
                updated_metadata.update(metadata)

            new_delta = delta if delta else old_sense.delta
            new_image = image if image is not None else old_sense.image_preview

            if current_item.sense.is_frozen:
                new_sense_id = generate_uuid7()

                # 1. Tạo AISense nháp mới (Kế thừa data cũ, cập nhật data mới)
                AISense.objects.create(
                    id=new_sense_id,
                    word_value=old_sense.word_value,
                    delta=new_delta,
                    image_preview=new_image,
                    metadata=updated_metadata,
                    created_by=current_user,
                    is_official=False,
                    is_frozen=False,
                    # Điểm cốt lõi: Gắn ID gốc để truy vết lịch sử của từ
                    original_id=old_sense.original_id or old_sense.id 
                )

            if current_item.released:
                # === TH 1: ĐÃ RELEASED -> NHÂN BẢN CẢ SENSE LẪN ITEM (GIỮ NGUYÊN ORIGINAL_ID) ===
                new_item_id = generate_uuid7()
                # 2. Tạo CollectionItem nháp mới nối vào Sense nháp mới
                CollectionItem.objects.create(
                    id=new_item_id,
                    collection_id=current_item.collection_id,
                    sense_id=new_sense_id,
                    # Điểm cốt lõi: Giữ nguyên original_id ở tầng Item để phục vụ distinct ở hàm retrieve
                    original_id=current_item.original_id or current_item.id, 
                    order=current_item.order,
                    released=False, # Trạng thái nháp
                    parent = list(current_item.parent or []).append(current_item.id),
                    is_deleted=False,
                    created_by=current_user
                )
                return_item_id = new_item_id

            else:
                # === TH 2: ĐANG LÀ BẢN NHÁP -> ĐƯỢC QUYỀN SỬA TRỰC TIẾP ===
                old_sense.delta = new_delta
                old_sense.image_preview = new_image
                old_sense.metadata = updated_metadata
                old_sense.save()
                return_item_id = current_item.id

        return Response(status=status.HTTP_200_OK, data={"message": "Success", "collection_item_id": return_item_id})
   
    @action(detail=True, methods=['post'], url_path='add-sense')
    def add_sense(self, request, pk=None, *args, **kwargs):
        sense_id = request.data.get('sense_id')
        user_collection_id = request.data.get('user_collection_id')

        if not sense_id or not user_collection_id:
            return Response(
                status=status.HTTP_400_BAD_REQUEST, 
                data={"message": "Missing sense_id or user_collection_id"}
            )

        current_user = request.user if request.user.is_authenticated else None

        with transaction.atomic():
            # 1. Khóa dòng UserCollection an toàn
            user_collection = get_object_or_404(
                UserCollection.objects.select_for_update(), 
                id=user_collection_id
            )
            
            # Lấy thông tin AISense mục tiêu để trích xuất original_id gốc
            target_sense = get_object_or_404(AISense.objects, id=sense_id)
            target_original_id = target_sense.original_id or target_sense.id

            # 2. KIỂM TRA TRÙNG LẶP: Quét theo trục original_id trong collection hiện tại
            # Lấy ra bản ghi mới nhất của từ này (nếu có) để check trạng thái
            existing_item = CollectionItem.objects.filter(
                collection_id=user_collection.collection_id,
                original_id=target_original_id
            ).filter(
                # Chỉ check các bản ghi có hiệu lực với user này
                Q(released=True, version__lte=user_collection.version) |
                Q(released=False, created_by=current_user)
            ).order_by("-released", "-version", "-created_at").first()

            # Nếu từ đã tồn tại VÀ chưa bị đánh dấu xóa (is_deleted=False) -> Báo lỗi trùng
            if existing_item and not existing_item.is_deleted:
                return Response(
                    status=status.HTTP_400_BAD_REQUEST, 
                    data={"message": "Item already exists in this version of collection"}
                )

            # 3. TÍNH TOÁN ORDER FLOAT (Xếp vào cuối danh sách)
            max_order_dict = CollectionItem.objects.filter(
                collection_id=user_collection.collection_id
            ).aggregate(max_order=Max('order'))
            
            current_max_order = max_order_dict.get('max_order') or 0.0
            new_order = float(current_max_order) + 1.0

            # 4. XỬ LÝ RE-ADD (Nếu từ từng bị xóa trước đó bằng cờ is_deleted=True)
            # Nếu trước đó từng bị xóa, ta tạo đè một dòng nháp mới khôi phục lại nó
            collection_item = CollectionItem.objects.create(
                id=generate_uuid7(),
                collection_id=user_collection.collection_id,
                sense_id=target_sense.id,
                original_id=target_original_id, # Đảm bảo neo giữ ID cốt lõi
                order=new_order,
                
                # Cấu hình trạng thái nháp
                released=False,                  # Hàng mới thêm luôn ở dạng nháp chờ Upload
                is_deleted=False,                # Bật lại trạng thái hoạt động
                created_by=current_user
            )

            # 5. LOGIC KIỂM TRA TỪ ĐÃ HỌC (PENDING LOGIC CỦA BẠN)
            # Giả sử bạn check xem User đã từng pass từ này ở các collection khác chưa
            # if has_learned_word(current_user, target_sense.word_value):
            #     user_collection.learned_words_count += 1
            #     user_collection.save()

        return Response(
            status=status.HTTP_200_OK, 
            data={"message": "Success", "item_id": collection_item.id}
        )

    @action(detail=True, methods=['post'], url_path='delete-sense')
    def delete_sense(self, request, pk=None, *args, **kwargs):
        collection_item_id = request.data.get('collection_item_id')
        user_collection_id = request.data.get('user_collection_id')

        if not collection_item_id or not user_collection_id:
            return Response(
                status=status.HTTP_400_BAD_REQUEST, 
                data={"message": "Missing collection_item_id or user_collection_id"}
            )

        current_user = request.user if request.user.is_authenticated else None

        with transaction.atomic():
            # 1. Khóa dòng UserCollection an toàn
            user_collection = get_object_or_404(
                UserCollection.objects.select_for_update(), 
                id=user_collection_id
            )
            
            # 2. Lấy CollectionItem mục tiêu cần xóa
            target_item = get_object_or_404(
                CollectionItem.objects.filter(collection_id=user_collection.collection_id),
                id=collection_item_id
            )

            return_item_id = target_item.id

            # === 3. PHÂN NHÁNH LOGIC XÓA THEO TRẠNG THÁI RELEASED ===
            if target_item.released:
                # --- TH 1: ITEM ĐÃ PHÁT HÀNH (RELEASED = TRUE) ---
                # Không được sửa dòng cũ, tạo một dòng Tombstone nháp để che nó đi
                return_item_id = generate_uuid7()
                
                CollectionItem.objects.create(
                    id=return_item_id,
                    collection_id=target_item.collection_id,
                    sense_id=target_item.sense_id,
                    # Điểm cốt lõi: Giữ nguyên original_id để hàm retrieve distinct biết khái niệm nào bị xóa
                    original_id=target_item.original_id or target_item.id,
                    order=target_item.order,
                    
                    # Cấu hình trạng thái xóa nháp
                    released=False,      # Vẫn nằm ở trạng thái nháp chờ Upload
                    is_deleted=True,     # Cắm cờ "Bia mộ" đánh dấu đã xóa
                    created_by=current_user
                )
            elif target_item.created_by == current_user:
                # -------------------------------------------------------------
                # TH 2: BẤM HỦY CHỈNH SỬA / XÓA TỪ MỚI TINH (RELEASED = FALSE)
                # -------------------------------------------------------------

                # TRUY TÌM BẢN GỐC: Quét ngược lịch sử xem từ này từng có bản phát hành chính thức nào không
                # Query đúng theo triết lý của hàm retrieve (Lấy bản released cao nhất <= version hiện tại của user)
                original_item = CollectionItem.objects.filter(
                    collection_id=user_collection.collection_id,
                    original_id=target_item.original_id,
                    released=True,
                    version__lte=user_collection.version
                ).select_related('sense').order_by("-version", "-created_at").first()

                # Tiến hành xóa cứng dòng nháp hiện tại khỏi Database
                target_item.delete()

                if original_item:
                    # CASE 2A: ĐÂY LÀ TỪ CŨ ĐƯỢC SỬA -> Trả về dữ liệu gốc để Front-end hiển thị lại
                    # (Bạn có thể dùng Serializer của bạn ở đây, ví dụ: CollectionItemSerializer(original_item).data)
                    reverted_data = {
                        "id": original_item.id,
                        "collection_id": original_item.collection_id,
                        "sense_id": original_item.sense_id,
                        "original_id": original_item.original_id,
                        "order": original_item.order,
                        "released": True,
                        "is_deleted": original_item.is_deleted,
                        "sense": {
                            "id": original_item.sense.id,
                            "word_value": original_item.sense.word_value,
                            "delta": original_item.sense.delta,
                            "image_preview": original_item.sense.image_preview
                        }
                    }
                    return Response(
                        status=status.HTTP_200_OK, 
                        data={
                            "action": "REVERTED_TO_ORIGINAL",
                            "message": "Draft cleared, reverted to original version",
                            "collection_item_id": original_item.id,
                            "reverted_item": reverted_data # Bản gốc quay trở lại!
                        }
                    )
                else:
                    # CASE 2B: ĐÂY LÀ TỪ MỚI TINH (User tự add nháp, chưa từng release bao giờ)
                    # Xóa nháp đồng nghĩa với việc từ này biến mất hoàn toàn khỏi bộ sưu tập
                    return Response(
                        status=status.HTTP_200_OK, 
                        data={
                            "action": "NEW_DRAFT_DELETED",
                            "message": "New draft word removed completely",
                            "collection_item_id": None,
                            "reverted_item": None # Biến mất hoàn toàn
                        }
                    )

            # 4. LOGIC KIỂM TRA TỪ ĐÃ HỌC (PENDING LOGIC CỦA BẠN)
            # Giả sử: Khi xóa từ khỏi bộ sưu tập, giảm số lượng từ đã học của user đi 1
            # if user_has_learned_this_item:
            #     user_collection.learned_words_count = max(0, user_collection.learned_words_count - 1)
            #     user_collection.save()

        return Response(
            status=status.HTTP_200_OK, 
            data={"message": "Success", "collection_item_id": return_item_id}
        )
    
    @action(detail=True, methods=['post'], url_path='clear-custom')
    def clear_custom(self, request, pk=None, *args, **kwargs):
        user_collection_id = request.data.get('user_collection_id')

        if not user_collection_id:
            return Response(
                status=status.HTTP_400_BAD_REQUEST, 
                data={"message": "Missing user_collection_id"}
            )

        current_user = request.user if request.user.is_authenticated else None

        with transaction.atomic():
            # 1. Lấy thông tin UserCollection để biết chính xác collection_id mục tiêu
            user_collection = get_object_or_404(
                UserCollection.objects.select_for_update(), 
                id=user_collection_id
            )

            # 2. XÓA CỨNG toàn bộ các bản ghi nháp (released=False) của user này
            # (Bao gồm cả draft update nội dung, draft add mới, và draft mang cờ is_deleted=True)
            deleted_count, _ = CollectionItem.objects.filter(
                collection_id=user_collection.collection_id,
                created_by=current_user,
                released=False
            ).delete()

            # Nếu không có dòng nháp nào để xóa, báo cho user biết hệ thống đã sạch sẵn rồi
            if deleted_count == 0:
                return Response(
                    status=status.HTTP_404_NOT_FOUND, 
                    data={"message": "No draft changes found to clear"}
                )

        return Response(
            status=status.HTTP_200_OK, 
            data={
                "message": "Success", 
                "user_collection_id": user_collection_id, 
                "cleared_count": deleted_count
            }
        )
    
    @action(detail=True, methods=['post'], url_path='upload')
    def upload(self, request, pk=None, *args, **kwargs):
        user_collection_id = request.data.get('user_collection_id')

        if not user_collection_id:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing user_collection_id"})

        current_user = request.user if request.user.is_authenticated else None

        with transaction.atomic():
            # 1. Khóa dòng UserCollection và lấy Collection gốc
            user_collection = get_object_or_404(
                UserCollection.objects.select_related('collection').select_for_update(), 
                id=user_collection_id
            )
            current_collection = user_collection.collection

            if not current_collection:
                return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "Current collection not found"})
            
            draft_items = CollectionItem.objects.filter(
                collection_id=current_collection.id,
                created_by=current_user,
                released=False
            )

            # Thay vì biến đếm int, ta chuyển thành mảng chứa ID
            added_ids = []
            updated_ids = []
            deleted_ids = []

            if draft_items.exists():
                past_original_ids = set(
                    CollectionItem.objects.filter(
                        collection_id=current_collection.id,
                        released=True
                    ).values_list('original_id', flat=True)
                )

                for item in draft_items:
                    # Lấy ID cốt lõi của từ
                    oid = str(item.original_id or item.id)
                    
                    if item.is_deleted:
                        deleted_ids.append(oid)
                    else:
                        if item.original_id in past_original_ids:
                            updated_ids.append(oid)
                        else:
                            added_ids.append(oid)

            # Đóng gói vào Metadata Snapshot
            next_version = current_collection.version + 1
            current_metadatas = list(current_collection.metadatas or [])

            last_meta = current_metadatas[-1] if current_metadatas else {}
            new_meta = {
                "version": next_version,
                "title": request.data.get('title') or last_meta.get('title') or current_collection.title,
                "desc": request.data.get('desc') or last_meta.get('desc') or current_collection.description,
                "image": request.data.get('image') or last_meta.get('image') or current_collection.image,
                
                # Lưu hẳn danh sách ID vào log luôn
                "changelog": {
                    "added": added_ids,
                    "updated": updated_ids,
                    "deleted": deleted_ids
                },
                "released_at": timezone.now().isoformat()
            }

            next_metadatas = current_metadatas.append(new_meta)

            # === PHÂN NHÁNH LOGIC THEO QUYỀN SỞ HỮU (TÁC GIẢ VS USER KHÁC) ===
            
            if current_user == current_collection.created_by:
                # =========================================================
                # CASE 1: TÁC GIẢ NÂNG CẤP VERSION (Tăng v, đóng gói nháp tại chỗ)
                # =========================================================
                
                # 1. Tăng version của Collection hiện tại lên 1 đơn vị
                next_version = current_collection.version + 1
                current_collection.version = next_version
                current_collection.metadatas = next_metadatas
                current_collection.save()

                # 2. Đóng gói toàn bộ hàng nháp (released=False) của tác giả thành chính thức
                # Gắn chặt vào số version mới nâng cấp
                CollectionItem.objects.filter(
                    collection_id=current_collection.id,
                    created_by=current_user,
                    released=False
                ).update(
                    released=True,
                    version=next_version
                )

                final_collection_id = current_collection.id
                final_user_version = next_version

            else:
                # =========================================================
                # CASE 2: USER KHÁC UPLOAD (Sinh bản mới hoàn toàn - Fork/Merge)
                # =========================================================
                new_collection_id = generate_uuid7()
                
                # 1. Thu thập "cây phả hệ" để quản lý vết tích như CivitAI
                current_parent_id = list(current_collection.parent or [])
                current_parent_id.append(current_collection.id)

                # 2. Lấy toàn bộ các item hợp lệ hiện tại của User khác này (Gốc + Nháp của họ)
                # Sử dụng đúng câu query distinct theo trục original_id giống hàm retrieve
                raw_items = CollectionItem.objects.filter(
                    collection_id=current_collection.id
                ).filter(
                    Q(released=True, version__lte=user_collection.version) |
                    Q(released=False, created_by=current_user)
                ).order_by("original_id", "-released", "-version", "-created_at").distinct("original_id")

                # Loại bỏ những item mang cờ xóa (is_deleted=True)
                valid_items = [item for item in raw_items if not item.is_deleted]

                # 3. Nhân bản dữ liệu sang Collection mới dưới dạng chính thức (released=True, v=1)
                new_items = [
                    CollectionItem(
                        id=generate_uuid7(),
                        collection_id=new_collection_id,
                        sense_id=item.sense_id,
                        original_id=item.original_id or item.id,
                        order=item.order,
                        released=True,         # Đóng gói chính thức ở nhà mới
                        version=1,             # Reset về v1 của nhánh mới
                        is_deleted=False,
                        created_by=current_user
                    )
                    for item in valid_items
                ]
                CollectionItem.objects.bulk_create(new_items)

                # 4. Dọn dẹp sạch sẽ các bản ghi nháp cũ của user này ở nhà cũ
                CollectionItem.objects.filter(
                    collection_id=current_collection.id, 
                    created_by=current_user, 
                    released=False
                ).delete()

                # 5. Tạo thực thể Collection mới ghi nhận chủ quyền mới
                Collection.objects.create(
                    id=new_collection_id,
                    title=f"{current_collection.title} - Remixed by {current_user.username if current_user else 'Anonymous'}",
                    description=current_collection.description,
                    created_by=current_user,
                    original_id=current_collection.original_id or current_collection.id,
                    parent=current_parent_id,
                    previous = current_collection.id,

                    metadatas = next_metadatas,

                    image=current_collection.image,
                    image_url=current_collection.image_url,
                    version=1, # Khởi tạo version 1
                    items_count=len(new_items)
                )

                final_collection_id = new_collection_id
                final_user_version = 1

            # === 3. CẬP NHẬT LẠI TRẠNG THÁI CHO USERCOLLECTION ===
            user_collection.collection_id = final_collection_id
            user_collection.version = final_user_version
            user_collection.save()

        return Response(
            status=status.HTTP_200_OK, 
            data={
                "message": "Upload success", 
                "collection_id": final_collection_id,
                "user_collection_id": user_collection.id,
                "current_version": final_user_version
            }
        )

    def perform_create(self, serializer):
        # Nếu bạn muốn gán user tạo collection
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()
