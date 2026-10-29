from rest_framework import serializers

from config.serializers.BaseModel import BaseModelSerializer
from utils.models.Tag import Tag

class TagSerializer(BaseModelSerializer):
    class Meta:
        model = Tag
        fields = [
            'id',
            'name', 'description',
            'languageCode', 'isSystemTag',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
