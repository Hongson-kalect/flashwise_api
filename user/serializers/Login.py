from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.UserSession import UserSession  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class LoginSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta(BaseModelSerializer.Meta):
        model = UserSession
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'device_id',
            'ip_address',
            'user_agent',
            'login_at',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + ['login_at']
