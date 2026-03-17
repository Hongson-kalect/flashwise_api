from rest_framework import serializers

from config.serializers.BaseModel import BaseModelSerializer
from utils.models.Topic import Topic

class TopicSerializer(BaseModelSerializer):
    class Meta:
        model = Topic
        fields = [
            'id', 'sub_id',
            'name', 'description',
            'languageCode',
            'isSystemTag', 'is_active', 'is_global',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
