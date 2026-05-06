from django.db.models import Prefetch, Count
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from core.models.Collection import Collection
from core.models.CollectionItem import CollectionItem
from core.models.UserCollection import UserCollection
from core.serializers.CollectionItem import CollectionItemSerializer
from utils.utils.soft_delete_viewset import SoftDeleteViewSet
from django.db import transaction

class CollectionItemViewSet(SoftDeleteViewSet):
    queryset = CollectionItem.objects.select_related('sense').all()
    serializer_class = CollectionItemSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def perform_create(self, serializer):
        # Nếu bạn muốn gán user tạo collection
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()
