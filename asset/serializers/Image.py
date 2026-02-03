from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from asset.models.Image import Image  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class ImageSerializer(BaseModelSerializer):
    uploader = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)

    class Meta(BaseModelSerializer.Meta):
        model = Image
        fields = BaseModelSerializer.Meta.fields + [
            'url',
            'uploader',
            'uploaded_at',
            'type',
            'type_display',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + ['uploaded_at']
