from rest_framework import serializers

from config.serializers.BaseModel import BaseModelSerializer
from core.models.WordInfo import WordInfo

class WordInfoSerializer(BaseModelSerializer):
    class Meta:
        model = WordInfo
        fields = [
            'id', 'sub_id',
            'usage', 'story', 'other_usage',
            'speak_tip', 'tip', 'pro_tip', 'remember_tip',
            'origin', 'is_deleted',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']