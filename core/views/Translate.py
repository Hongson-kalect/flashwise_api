from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from core.models import Word, WordInfo
from django.contrib.auth import get_user_model

from core.models.Translate import Translate

User = get_user_model()

class TranslateSerializer(BaseModelSerializer):
    detail = serializers.PrimaryKeyRelatedField(queryset=Word.objects.all())
    info = serializers.PrimaryKeyRelatedField(queryset=WordInfo.objects.all(), allow_null=True, required=False)
    request_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True, required=False)

    class Meta:
        model = Translate
        fields = [
            'id', 'sub_id', 'title', 'language_code', 'is_auto',
            'detail', 'info', 'example',
            'is_deleted', 'is_active', 'request_by',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
