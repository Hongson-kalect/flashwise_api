from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from django.contrib.auth import get_user_model

from core.models.UserCollection import UserCollection
from core.serializers.CollectionItem import CollectionItemSerializer,CollectionItemBasicSerializer
from core.serializers.Collection import CollectionBasicSerializer

User = get_user_model()

class UserCollectionSerializer(BaseModelSerializer):
    items = CollectionItemBasicSerializer(many=True, read_only=True)
    collection = CollectionBasicSerializer(read_only=True)

    class Meta:
        model = UserCollection
        fields = ['id', 'sub_id','collection', 'created_at', 'updated_at', 'items','learned_count']
        read_only_fields = ['id', 'sub_id','collection', 'created_at', 'updated_at', 'items']
