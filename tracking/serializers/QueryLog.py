from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from tracking.models.QueryLog import QueryLog

User = get_user_model()

class QueryLogSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta:
        model = QueryLog
        fields = [
            'id', 'sub_id',
            'user', 'target_type', 'target_id',
            'meta', 'is_success',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
