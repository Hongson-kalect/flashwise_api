from rest_framework import serializers

from ai.models.AISense import AISense
from ai.models.AISenseContent import AISenseContent
from ai.models.AIWord import AIWord
from ai.serializers.AISense import AISenseSerializer
from ai.serializers.AISenseMetadata import AISenseMetadataSerializer
from config.serializers.BaseModel import BaseModelSerializer

class AIWordSerializer(BaseModelSerializer):
    senses = AISenseSerializer(many=True, read_only=True,source='prefetched_senses')

    # entries = serializers.SerializerMethodField()
    # def get_entries(self, obj):
    #     return getattr(obj, 'processed_entries', [])

    class Meta:
        model = AIWord
        fields = ['id', 'value', 'language_code', "senses"]
    # contents = serializers.JSONField()
    # class Meta:
    #     model = AIWord
    #     fields = "__all__"
    #     read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']