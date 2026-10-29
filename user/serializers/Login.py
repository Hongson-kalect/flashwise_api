from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.UserSession import UserSession

User = get_user_model()

class LoginSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta(BaseModelSerializer.Meta):
        model = UserSession
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'device',
            'ip_address',
            'client_name',
            'last_active_at',
            'is_active',
            'created_at',
        ]
        # Không trả refresh_token_jti ra ngoài
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + ['last_active_at']
