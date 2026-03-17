from rest_framework import serializers

from ai.models.AISenseMetadata import AISenseMetadata
from config.serializers.BaseModel import BaseModelSerializer

class AISenseMetadataSerializer(BaseModelSerializer):
    class Meta:
        model = AISenseMetadata
        fields = "__all__"
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']