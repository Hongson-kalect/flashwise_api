from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from core.models.Collection import Collection
from interact.models.ViewCollection import ViewCollection

User = get_user_model()

class ViewCollectionSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    collection = serializers.PrimaryKeyRelatedField(queryset=Collection.objects.all())

    class Meta:
        model = ViewCollection
        fields = [
            'id', 'sub_id',
            'user', 'collection',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def validate(self, data):
        user = data.get('user')
        collection = data.get('collection')
        if ViewCollection.objects.filter(user=user, collection=collection).exists():
            raise serializers.ValidationError("User has already viewed this collection.")
        return data
