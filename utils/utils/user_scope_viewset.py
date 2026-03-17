from rest_framework import viewsets, status
from rest_framework.response import Response

from utils.utils.soft_delete_viewset import SoftDeleteViewSet


# Tạm không dùng cái này
class UserScopeViewSet(SoftDeleteViewSet):
    # def get_queryset(self):
    def list(self):
        queryset = super().get_queryset()
        model = self.queryset.model

        # Lọc soft delete và trạng thái
        if hasattr(model, 'is_deleted'):
            queryset = queryset.filter(is_deleted=False)
        if hasattr(model, 'is_active'):
            queryset = queryset.filter(is_active=True)


        # Lọc theo người dùng hiện tại
        if(self.request.user.is_authenticated):
            if hasattr(model, 'user'):
                queryset = queryset.filter(user=self.request.user)
            elif hasattr(model, 'updated_by'):
                queryset = queryset.filter(updated_by=self.request.user)
            elif hasattr(model, 'created_by'):
                queryset = queryset.filter(created_by=self.request.user)


        return queryset
