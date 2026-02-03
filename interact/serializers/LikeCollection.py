from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from core.models.Collection import Collection
from interact.models.LikeCollection import LikeCollection

User = get_user_model()

class LikeCollectionSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    collection = serializers.PrimaryKeyRelatedField(queryset=Collection.objects.all(), allow_null=True, required=False)

    class Meta:
        model = LikeCollection
        fields = [
            'id', 'sub_id',
            'user', 'collection', 'collection_sub_id',
            'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def validate(self, data):
        user = data.get('user')
        collection_sub_id = data.get('collection_sub_id')
        if LikeCollection.objects.filter(user=user, collection_sub_id=collection_sub_id).exists():
            raise serializers.ValidationError("User has already liked this collection.")
        return data
