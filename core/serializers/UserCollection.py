from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from core.models import Collection, UploadedCollection
from django.contrib.auth import get_user_model

from core.models.UserCollection import UserCollection

User = get_user_model()

class UserCollectionSerializer(BaseModelSerializer):

    class Meta:
        model = UserCollection
        fields = "__all__"
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
