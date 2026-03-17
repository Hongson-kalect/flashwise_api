from rest_framework import serializers

from config.serializers.BaseModel import BaseModelSerializer
from core.models.WordInfo import WordInfo

class WordInfoSerializer(BaseModelSerializer):
    class Meta:
        model = WordInfo
        fields = "__all__"
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

class BasicWordInfoSerializer(BaseModelSerializer):
    class Meta:
        model = WordInfo
        fields = [
            'pos',
            'ipas',
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']