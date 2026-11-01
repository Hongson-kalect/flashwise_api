import json

from django.utils import timezone
from django.db.models import Prefetch, Q, Max
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from ai.models import AISense, AIWord, AISenseMetadata
from ai.queries import visible_senses
from ai.sense_edit import apply_sense_edit
from ai.validators import MAX_DELTA_CHARS, is_uuid
from core.serializers import CollectionDetailSerializer
from core.models import Collection,CollectionItem, UserCollection
from core.queries import visible_collections
from core.serializers.UserCollection import UserCollectionSerializer
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from utils.utils.sense_handle import deep_merge
from utils.utils.uuidv7 import generate_uuid7
from django.db import transaction
from rest_framework.decorators import action
from django.contrib.auth.models import User
from django.forms.models import model_to_dict
from django.db.models import Prefetch, Case, When, F, Value, Func, ExpressionWrapper, IntegerField

def visible_items(user_collection, user):
    """Các dòng user này nhìn thấy trong bộ: bản đã phát hành tới version user đang dùng, và dòng nháp của chính user."""
    return CollectionItem.objects.filter(collection_id=user_collection.collection_id).filter(
        Q(released=True, version__lte=user_collection.version) | Q(released=False, created_by=user)
    )


def get_visible_item(user_collection, user, item_id):
    return get_object_or_404(visible_items(user_collection, user).select_related('sense'), id=item_id)


def count_released_items(collection_id):
    """Số từ của bản phát hành mới nhất (mỗi từ lấy dòng version cao nhất, bỏ từ đã xoá)."""
    flags = (
        CollectionItem.objects.filter(collection_id=collection_id, released=True)
        .order_by('original_id', '-version', '-created_at').distinct('original_id').values_list('is_deleted', flat=True)
    )
    return sum(1 for deleted in flags if not deleted)


def freeze_senses(item_queryset):
    """Nghĩa đã phát hành thì không sửa tại chỗ nữa: sửa sẽ tách bản mới, bộ của người khác không bị đổi theo."""
    AISense.objects.filter(id__in=list(item_queryset.values_list('sense_id', flat=True)), is_frozen__in=[False, None]).update(is_frozen=True)


class UserCollectionViewSet(SoftDeleteViewSet):
    queryset = UserCollection.objects.all()
    serializer_class = UserCollectionSerializer
    # Bộ từ cá nhân: bắt buộc đăng nhập, chỉ thấy / sửa được của chính mình
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return super().get_queryset().filter(user=self.request.user)

    # Giả sử nằm trong CollectionViewSet
    @action(detail=True, methods=['post'], url_path='download')
    def download(self, request, pk=None):
        # pk ở URL là id của Collection gốc (không phải UserCollection)
        collection = get_object_or_404(visible_collections(request.user), pk=pk)
        
        # Tạo hoặc cập nhật trạng thái sở hữu
        user_col, created = UserCollection.objects.get_or_create(
            user=request.user,
            collection=collection,
            is_deleted=False,
            defaults={'name': collection.name, 'description': collection.description, 'created_by': request.user, 'version': collection.version},
        )
        
        if not created:
            return Response({"detail": "Bạn đã sở hữu bộ sưu tập này rồi."}, status=200)

        Collection.objects.filter(pk=collection.pk).update(downloaded_count=F('downloaded_count') + 1)
        return Response({"detail": "Tải bộ sưu tập thành công."}, status=201)
    
    # @action(detail=True, methods=['post'], url_path='get-collection')
    # def get_user_collection_detail(self, request, pk=None):
        
    def create(self, request, *args, **kwargs):
        # Bộ từ mới tạo qua /api/collections/ (từ nghĩa có sẵn) hoặc /api/collections/bulk-create/ (từ danh sách từ);
        # cả hai đã tự tạo UserCollection cho người tạo, tải bộ có sẵn dùng download.
        return Response(
            {"message": "Use POST /api/collections/ or /api/collections/bulk-create/ to create a collection"},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

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
            self.get_queryset().select_related('collection'), 
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
            "released",    # Hàng nháp (False) đứng trước hàng đã release (True) để nháp thắng ở distinct
            "-version",    # Lấy version mới nhất đè lên version cũ
            "-created_at"  # Sắp xếp theo thời gian tạo mới nhất nếu trùng version
        ).distinct("original_id").select_related('sense')

        # Loại bỏ các item mang cờ xóa (Tombstone)
        # Sau đó sắp xếp lại danh sách cuối cùng theo trục 'order' (float) để hiển thị lên UI cho đúng thứ tự kéo thả
        final_items = sorted(
            [item for item in raw_items if not item.is_deleted],
            key=lambda x: x.display_order
        )

        # Gán danh sách item sạch sẽ vào thuộc tính tạm để Serializer đóng gói trả về cho Client
        user_col.items = final_items

        return Response(self.get_serializer(user_col).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='update-sense')
    def update_sense(self, request, *args, **kwargs):
        """
        Sửa nghĩa trong bộ cá nhân. Body: user_collection_id, collection_item_id, delta, metadata, image.
        Dòng đã phát hành không bị sửa: tạo (hoặc dùng lại) dòng nháp của user trỏ tới nghĩa đã sửa. Dòng nháp thì đổi thẳng nghĩa.
        Bản sửa nghĩa dùng chung logic với ai-senses/update-sense (nghĩa chính thức / đã đóng băng / của người khác được tách thành bản mới).
        """
        user = request.user
        item_id = request.data.get('collection_item_id')
        user_collection_id = request.data.get('user_collection_id')
        if not is_uuid(item_id) or not is_uuid(user_collection_id):
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing or invalid IDs"})

        delta = request.data.get('delta') or {}
        metadata = request.data.get('metadata')
        image = request.data.get('image')
        if not isinstance(delta, dict) or len(json.dumps(delta)) > MAX_DELTA_CHARS:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Invalid delta"})
        if image is not None and (not isinstance(image, str) or len(image) > 1000):
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Invalid image"})

        with transaction.atomic():
            user_collection = get_object_or_404(self.get_queryset().select_for_update(), id=user_collection_id)
            item = get_visible_item(user_collection, user, item_id)
            if item.is_deleted:
                return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Item is deleted"})

            new_sense = apply_sense_edit(user, item.sense, delta, metadata, image)

            if item.released:
                draft = CollectionItem.objects.filter(
                    collection_id=item.collection_id, original_id=item.original_id, created_by=user, released=False,
                ).first()
                if draft is None:
                    draft = CollectionItem.objects.create(
                        collection_id=item.collection_id, sense=new_sense, original_id=item.original_id or item.sense_id,
                        value=item.value, display_order=item.display_order, released=False, is_deleted=False,
                        parent=[*(item.parent or []), item.id], created_by=user,
                    )
                else:
                    draft.sense, draft.is_deleted = new_sense, False
                    draft.save(update_fields=['sense', 'is_deleted', 'updated_at'])
                result = draft
            else:
                item.sense = new_sense
                item.save(update_fields=['sense', 'updated_at'])
                result = item

        return Response(status=status.HTTP_200_OK, data={"message": "Success", "collection_item_id": result.id, "sense_id": new_sense.id})

    @action(detail=True, methods=['post'], url_path='add-sense')
    def add_sense(self, request, pk=None, *args, **kwargs):
        """Thêm một nghĩa (chính thức hoặc của chính mình) vào bộ dưới dạng dòng nháp. Body: user_collection_id, sense_id."""
        user = request.user
        sense_id = request.data.get('sense_id')
        user_collection_id = request.data.get('user_collection_id')
        if not is_uuid(sense_id) or not is_uuid(user_collection_id):
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing sense_id or user_collection_id"})

        with transaction.atomic():
            user_collection = get_object_or_404(self.get_queryset().select_for_update(), id=user_collection_id)

            # Nghĩa chính thức, nghĩa của mình hoặc nghĩa người khác đã upload
            target_sense = get_object_or_404(
                visible_senses(user), id=sense_id,
            )
            oid = target_sense.original_id or target_sense.id

            def latest():
                return visible_items(user_collection, user).filter(original_id=oid).order_by("released", "-version", "-created_at").first()

            existing = latest()
            if existing and not existing.is_deleted:
                return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Item already exists in this version of collection"})

            # Từ từng bị xoá nháp: bỏ dòng xoá thì bản đã phát hành hiện lại, không cần thêm dòng mới
            CollectionItem.objects.filter(
                collection_id=user_collection.collection_id, original_id=oid, created_by=user, released=False, is_deleted=True,
            ).delete()
            restored = latest()
            if restored and not restored.is_deleted:
                return Response(status=status.HTTP_200_OK, data={"message": "Success", "item_id": restored.id, "restored": True})

            last_order = CollectionItem.objects.filter(collection_id=user_collection.collection_id).aggregate(m=Max('display_order'))['m'] or 0.0
            item = CollectionItem.objects.create(
                collection_id=user_collection.collection_id, sense=target_sense, original_id=oid, value=target_sense.word_value,
                display_order=float(last_order) + 1.0, released=False, is_deleted=False, created_by=user,
            )

        return Response(status=status.HTTP_200_OK, data={"message": "Success", "item_id": item.id})

    @action(detail=True, methods=['post'], url_path='delete-sense')
    def delete_sense(self, request, pk=None, *args, **kwargs):
        """
        Xoá một dòng khỏi bộ. Body: user_collection_id, collection_item_id.
        - Dòng đã phát hành: tạo dòng nháp "bia mộ" (is_deleted) che nó đi, bản phát hành giữ nguyên cho người dùng bản cũ.
        - Dòng nháp: xoá hẳn dòng nháp; nếu từng có bản phát hành thì quay về bản đó, không thì từ biến mất.
        """
        user = request.user
        item_id = request.data.get('collection_item_id')
        user_collection_id = request.data.get('user_collection_id')
        if not is_uuid(item_id) or not is_uuid(user_collection_id):
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing collection_item_id or user_collection_id"})

        with transaction.atomic():
            user_collection = get_object_or_404(self.get_queryset().select_for_update(), id=user_collection_id)
            target = get_visible_item(user_collection, user, item_id)

            if target.released:
                # Chỉ giữ 1 dòng nháp cho mỗi từ: bỏ dòng nháp sửa (nếu có) rồi đặt bia mộ
                CollectionItem.objects.filter(
                    collection_id=target.collection_id, original_id=target.original_id, created_by=user, released=False,
                ).delete()
                tombstone = CollectionItem.objects.create(
                    collection_id=target.collection_id, sense_id=target.sense_id, original_id=target.original_id or target.sense_id,
                    value=target.value, display_order=target.display_order, released=False, is_deleted=True, created_by=user,
                )
                return Response(status=status.HTTP_200_OK, data={"message": "Success", "collection_item_id": tombstone.id})

            # Dòng nháp của chính mình (get_visible_item đã loại nháp của người khác)
            original_item = CollectionItem.objects.filter(
                collection_id=target.collection_id, original_id=target.original_id, released=True, version__lte=user_collection.version,
            ).select_related('sense').order_by("-version", "-created_at").first()
            target.delete()

            if original_item and not original_item.is_deleted:
                return Response(status=status.HTTP_200_OK, data={
                    "action": "REVERTED_TO_ORIGINAL",
                    "message": "Draft cleared, reverted to original version",
                    "collection_item_id": original_item.id,
                    "reverted_item": {
                        "id": original_item.id, "collection_id": original_item.collection_id, "sense_id": original_item.sense_id,
                        "original_id": original_item.original_id, "display_order": original_item.display_order,
                        "released": True, "is_deleted": False,
                        "sense": {"id": original_item.sense.id, "word_value": original_item.sense.word_value,
                                  "delta": original_item.sense.delta, "image_preview": original_item.sense.image_preview},
                    },
                })
            return Response(status=status.HTTP_200_OK, data={
                "action": "NEW_DRAFT_DELETED", "message": "New draft word removed completely",
                "collection_item_id": None, "reverted_item": None,
            })

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
                self.get_queryset().select_for_update(), 
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
                self.get_queryset().select_related('collection').select_for_update(of=('self',)), 
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
            if not draft_items.exists():
                return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "No changes to upload"})

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
                "title": request.data.get('title') or last_meta.get('title') or current_collection.name,
                "desc": request.data.get('desc') or last_meta.get('desc') or current_collection.description,
                "image": request.data.get('image') or last_meta.get('image') or current_collection.image_url,
                
                # Lưu hẳn danh sách ID vào log luôn
                "changelog": {
                    "added": added_ids,
                    "updated": updated_ids,
                    "deleted": deleted_ids
                },
                "released_at": timezone.now().isoformat()
            }

            next_metadatas = [*current_metadatas, new_meta]

            # === PHÂN NHÁNH LOGIC THEO QUYỀN SỞ HỮU (TÁC GIẢ VS USER KHÁC) ===
            
            if current_user == current_collection.created_by:
                # =========================================================
                # CASE 1: TÁC GIẢ NÂNG CẤP VERSION (Tăng v, đóng gói nháp tại chỗ)
                # =========================================================
                
                # 1. Tăng version của Collection hiện tại lên 1 đơn vị
                next_version = current_collection.version + 1
                current_collection.version = next_version
                current_collection.metadatas = next_metadatas
                current_collection.released = True
                current_collection.is_uploaded = True
                current_collection.save()

                # 2. Đóng gói toàn bộ hàng nháp (released=False) của tác giả thành chính thức
                # Gắn chặt vào số version mới nâng cấp
                promoted = CollectionItem.objects.filter(
                    collection_id=current_collection.id,
                    created_by=current_user,
                    released=False
                )
                freeze_senses(promoted.filter(is_deleted=False))
                promoted.update(released=True, version=next_version)
                current_collection.item_count = count_released_items(current_collection.id)
                current_collection.save(update_fields=['item_count', 'updated_at'])

                final_collection_id = current_collection.id
                final_user_version = next_version

            else:
                # =========================================================
                # CASE 2: USER KHÁC UPLOAD (Sinh bản mới hoàn toàn - Fork/Merge)
                # =========================================================
                new_collection_id = generate_uuid7()
                
                # 1. Thu thập "cây phả hệ" để quản lý vết tích như CivitAI
                current_parent_id = list(current_collection.parents or [])
                current_parent_id.append(current_collection.id)

                # 2. Lấy toàn bộ các item hợp lệ hiện tại của User khác này (Gốc + Nháp của họ)
                # Sử dụng đúng câu query distinct theo trục original_id giống hàm retrieve
                raw_items = CollectionItem.objects.filter(
                    collection_id=current_collection.id
                ).filter(
                    Q(released=True, version__lte=user_collection.version) |
                    Q(released=False, created_by=current_user)
                ).order_by("original_id", "released", "-version", "-created_at").distinct("original_id")

                # Loại bỏ những item mang cờ xóa (is_deleted=True)
                valid_items = [item for item in raw_items if not item.is_deleted]

                # 3. Nhân bản dữ liệu sang Collection mới dưới dạng chính thức (released=True, v=1)
                new_items = [
                    CollectionItem(
                        id=generate_uuid7(),
                        collection_id=new_collection_id,
                        sense_id=item.sense_id,
                        original_id=item.original_id or item.id,
                        display_order=item.display_order,
                        released=True,         # Đóng gói chính thức ở nhà mới
                        version=1,             # Reset về v1 của nhánh mới
                        is_deleted=False,
                        created_by=current_user
                    )
                    for item in valid_items
                ]
                freeze_senses(CollectionItem.objects.filter(id__in=[item.id for item in valid_items]))
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
                    name=f"{current_collection.name} - Remixed by {current_user.username if current_user else 'Anonymous'}",
                    description=current_collection.description,
                    created_by=current_user,
                    original_id=current_collection.original_id or current_collection.id,
                    parents=current_parent_id,
                    previous_id=current_collection.id,

                    metadatas = next_metadatas,

                    image=current_collection.image,
                    image_url=current_collection.image_url,
                    version=1, # Khởi tạo version 1
                    released=True,
                    is_uploaded=True,
                    item_count=len(new_items),
                    language_code=current_collection.language_code
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
