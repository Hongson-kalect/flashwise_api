from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from django.contrib.auth import get_user_model

from core.models.UserCollection import UserCollection
from ai.serializers.AISense import AISenseSerializerBasic

User = get_user_model()

class UserCollectionSerializer(BaseModelSerializer):
    senses = AISenseSerializerBasic(many=True, read_only=True)

    class Meta:
        model = UserCollection
        fields = "__all__"
        read_only_fields = ['id', 'sub_id','collection_id', 'created_at', 'updated_at', 'senses']
