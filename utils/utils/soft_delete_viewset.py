from rest_framework.decorators import action
from rest_framework import viewsets, status
from rest_framework.response import Response

class SoftDeleteViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        queryset = super().get_queryset()
        if hasattr(self.queryset.model, 'is_deleted'):
            queryset = queryset.filter(is_deleted=False)
        if hasattr(self.queryset.model, 'is_active'):
            queryset = queryset.filter(is_active=True)
        return queryset

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if hasattr(instance, 'is_deleted'):
            instance.is_deleted = True

            if hasattr(instance, 'deleted_at'):
                instance.deleted_at = instance.updated_at
                
            instance.save()
            return Response(status=status.HTTP_204_NO_CONTENT)
        return super().destroy(request, *args, **kwargs)
    
    def perform_create(self, serializer):
        user = self.request.user if self.request.user.is_authenticated else None

        print('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', user)
        if hasattr(serializer.Meta.model, 'created_by') and user:
            serializer.save(created_by=user, updated_by=user)
        else:
            serializer.save()

    def perform_update(self, serializer):
        instance = self.get_object()
        user = self.request.user if self.request.user.is_authenticated else None
        model = serializer.Meta.model

        # Kiểm tra có trường sub_id không
        if hasattr(model, 'sub_id'):
            # Vô hiệu hóa bản ghi hiện tại nếu là bản cũ của bản thân
            if(instance.updated_by == user):
                instance.is_active = False
                instance.updated_by = user
                instance.updated_at = timezone.now()
                instance.save()

            # Tạo bản ghi mới (version mới)
            data = serializer.validated_data.copy()
            data['sub_id'] = instance.sub_id
            data['created_by'] = instance.created_by
            data['updated_by'] = user
            model.objects.create(**data)
        else:
            # Cập nhật trực tiếp nếu không có versioning
            serializer.save(updated_by=user)
    
    @action(detail=False, methods=['delete'], url_path='bulk-delete')
    def bulk_delete(self, request):
        ids = request.data.get('ids', [])
        if not isinstance(ids, list):
            return Response({'detail': 'ids phải là một danh sách.'}, status=400)

        # Lấy queryset đã lọc theo quyền sở hữu, is_deleted, is_active...
        count =self.get_queryset().filter(id__in=ids).update(is_deleted=True, deleted_at=timezone.now())

        return Response({'deleted': count}, status=status.HTTP_200_OK)
    
    @action(detail=False, methods=['get'], url_path='test-connect')
    def test_connet(self, request):
        return Response({'detail': 'ok'}, status=status.HTTP_200_OK)
