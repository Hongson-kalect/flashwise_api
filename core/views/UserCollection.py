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
from core.queries import live_items, visible_collections
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


def classify_drafts(collection_id, user, base_version):
    """
    So từng dòng nháp của user với bản phát hành mới nhất tới base_version của cùng từ (original_id).
    Trả ({added, updated, deleted}: original_id thật sự đổi, [id dòng nháp không đổi gì]).
    Không đổi: cùng sense_id với bản đang có, hoặc xoá từ vốn không có / đã bị xoá.
    """
    drafts = list(CollectionItem.objects.filter(collection_id=collection_id, created_by=user, released=False))
    base = {
        row.original_id: row
        for row in CollectionItem.objects.filter(
            collection_id=collection_id, released=True, version__lte=base_version, original_id__in=[d.original_id for d in drafts],
        ).order_by('original_id', '-version', '-created_at').distinct('original_id')
    }
    changes, noop = {"added": [], "updated": [], "deleted": []}, []
    for draft in drafts:
        current = base.get(draft.original_id)
        live = current is not None and not current.is_deleted
        oid = str(draft.original_id or draft.sense_id)
        if draft.is_deleted:
            if live:
                changes["deleted"].append(oid)
            else:
                noop.append(draft.id)
        elif not live:
            changes["added"].append(oid)
        elif current.sense_id != draft.sense_id:
            changes["updated"].append(oid)
        else:
            noop.append(draft.id)
    return changes, noop


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

        # Mỗi từ: nháp của chính user thắng, không thì bản phát hành mới nhất tới version user đang dùng; bỏ dòng xoá
        final_items = live_items(collection.id, user_col.version, current_user)

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
    
    def _conflicts(self, user_collection, user, upto_version):
        """Từ user đang có nháp mà tác giả đã đổi ở các bản phát hành sau version user đang dùng (nháp vẫn thắng)."""
        drafted = CollectionItem.objects.filter(
            collection_id=user_collection.collection_id, created_by=user, released=False,
        ).values_list('original_id', flat=True)
        changed = CollectionItem.objects.filter(
            collection_id=user_collection.collection_id, released=True,
            version__gt=user_collection.version, version__lte=upto_version, original_id__in=drafted,
        ).values_list('original_id', flat=True).distinct()
        return [str(original_id) for original_id in changed]

    @action(detail=True, methods=['get'], url_path='updates')
    def updates(self, request, pk=None):
        """Bộ đã tải có bản phát hành mới hơn không: version đang dùng / mới nhất, changelog các bản mới, từ bị xung đột với nháp."""
        user_collection = get_object_or_404(self.get_queryset().select_related('collection'), id=pk)
        collection = user_collection.collection
        if collection is None:
            return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "Collection not found"})
        return Response({
            "current_version": user_collection.version,
            "latest_version": collection.version,
            "has_update": collection.version > user_collection.version,
            "changes": [meta for meta in (collection.metadatas or []) if (meta.get('version') or 0) > user_collection.version],
            "conflicts": self._conflicts(user_collection, request.user, collection.version),
        })

    @action(detail=True, methods=['post'], url_path='update-version')
    def update_version(self, request, pk=None):
        """
        Nâng bộ đã tải lên bản phát hành mới nhất. Nháp của user được giữ và vẫn thắng bản của tác giả;
        các từ đó trả về trong conflicts để app báo (user xoá nháp thì nhận bản của tác giả).
        """
        with transaction.atomic():
            user_collection = get_object_or_404(self.get_queryset().select_related('collection').select_for_update(of=('self',)), id=pk)
            collection = user_collection.collection
            if collection is None:
                return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "Collection not found"})
            previous = user_collection.version
            if collection.version <= previous:
                return Response({"updated": False, "current_version": previous, "conflicts": []})
            conflicts = self._conflicts(user_collection, request.user, collection.version)
            user_collection.version = collection.version
            user_collection.save(update_fields=['version', 'updated_at'])
        return Response({"updated": True, "previous_version": previous, "current_version": collection.version, "conflicts": conflicts})

    @action(detail=True, methods=['post'], url_path='upload')
    def upload(self, request, pk=None, *args, **kwargs):
        """
        Phát hành nháp của user. Chỉ dòng nháp thật sự đổi nghĩa so với bản đang dùng mới được tính (thêm / đổi sense_id / xoá);
        nháp không đổi gì bị dọn đi, không còn gì thật sự đổi thì trả 400 no_changes và không tạo version.
        - Tác giả: tăng version của bộ, nháp thành bản phát hành, title / desc / image (nếu gửi) thành thông tin bộ.
        - Người khác: tách thành bộ remix mới (v1) gồm bản đang dùng + nháp của họ.
        Body: user_collection_id, title, desc, image (tuỳ chọn).
        """
        user_collection_id = request.data.get('user_collection_id')
        if not is_uuid(user_collection_id):
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "Missing user_collection_id"})
        current_user = request.user
        for key, limit in (('title', 255), ('desc', 2000), ('image', 1000)):
            value = request.data.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": f"Invalid {key}"})

        with transaction.atomic():
            user_collection = get_object_or_404(
                self.get_queryset().select_related('collection').select_for_update(of=('self',)), id=user_collection_id,
            )
            current_collection = user_collection.collection
            if not current_collection:
                return Response(status=status.HTTP_404_NOT_FOUND, data={"message": "Current collection not found"})

            is_author = current_user.id == current_collection.created_by_id
            base_version = current_collection.version if is_author else user_collection.version
            changes, noop_ids = classify_drafts(current_collection.id, current_user, base_version)
            if noop_ids:
                CollectionItem.objects.filter(id__in=noop_ids).delete()
            if not any(changes.values()):
                return Response(status=status.HTTP_400_BAD_REQUEST, data={"message": "No changes to upload", "errorCode": "no_changes"})

            next_version = current_collection.version + 1
            current_metadatas = list(current_collection.metadatas or [])
            last_meta = current_metadatas[-1] if current_metadatas else {}
            new_meta = {
                "version": next_version,
                "title": request.data.get('title') or last_meta.get('title') or current_collection.name,
                "desc": request.data.get('desc') or last_meta.get('desc') or current_collection.description,
                "image": request.data.get('image') or last_meta.get('image') or current_collection.image_url,
                "changelog": changes,
                "released_at": timezone.now().isoformat(),
            }

            if is_author:
                # Tác giả: nháp (đã dọn dòng không đổi) thành bản phát hành của version mới
                current_collection.version = next_version
                current_collection.metadatas = [*current_metadatas, new_meta]
                current_collection.name = new_meta['title']
                current_collection.description = new_meta['desc'] or ''
                current_collection.image_url = new_meta['image']
                current_collection.released = True
                current_collection.is_uploaded = True
                promoted = CollectionItem.objects.filter(collection_id=current_collection.id, created_by=current_user, released=False)
                freeze_senses(promoted.filter(is_deleted=False))
                promoted.update(released=True, version=next_version)
                current_collection.item_count = count_released_items(current_collection.id)
                current_collection.save()
                final_collection_id = current_collection.id
                final_user_version = next_version
            else:
                # Người khác: tách bộ remix mới gồm bản đang dùng + nháp của họ
                new_collection_id = generate_uuid7()
                valid_items = live_items(current_collection.id, user_collection.version, current_user)
                new_items = [
                    CollectionItem(
                        id=generate_uuid7(), collection_id=new_collection_id, sense_id=item.sense_id,
                        original_id=item.original_id or item.sense_id, value=item.value, display_order=item.display_order,
                        released=True, version=1, is_deleted=False, created_by=current_user,
                    )
                    for item in valid_items
                ]
                freeze_senses(CollectionItem.objects.filter(id__in=[item.id for item in valid_items]))
                Collection.objects.create(
                    id=new_collection_id,
                    name=request.data.get('title') or f"{current_collection.name} - Remixed by {current_user.username}",
                    description=new_meta['desc'] or '',
                    created_by=current_user,
                    original_id=current_collection.original_id or current_collection.id,
                    parents=[*(current_collection.parents or []), current_collection.id],
                    previous_id=current_collection.id,
                    metadatas=[{**new_meta, "version": 1}],
                    image=current_collection.image,
                    image_url=new_meta['image'],
                    version=1, released=True, is_uploaded=True,
                    item_count=len(new_items),
                    language_code=current_collection.language_code,
                )
                CollectionItem.objects.bulk_create(new_items)
                CollectionItem.objects.filter(collection_id=current_collection.id, created_by=current_user, released=False).delete()
                final_collection_id = new_collection_id
                final_user_version = 1

            user_collection.collection_id = final_collection_id
            user_collection.version = final_user_version
            user_collection.save()

        return Response(status=status.HTTP_200_OK, data={
            "message": "Upload success",
            "collection_id": final_collection_id,
            "user_collection_id": user_collection.id,
            "current_version": final_user_version,
            "changelog": changes,
        })

    def perform_create(self, serializer):
        # Nếu bạn muốn gán user tạo collection
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()
