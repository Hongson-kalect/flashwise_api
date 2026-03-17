from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.RestrictList import RestrictList  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class RestrictListSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    target = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta(BaseModelSerializer.Meta):
        model = RestrictList
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'target',
            'reason',
            'created_at',
            'is_muted',
            'is_blocked',
            'metadata',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + ['created_at']
