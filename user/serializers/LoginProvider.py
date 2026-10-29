from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.AccountProvider import AccountProvider

User = get_user_model()

class LoginProviderSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(read_only=True)
    provider_display = serializers.CharField(source='get_provider_display', read_only=True)

    class Meta(BaseModelSerializer.Meta):
        model = AccountProvider
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'provider',
            'provider_display',
            'provider_user_id',
            'profile_url',
            'created_at',
        ]
        # Không trả access_token ra ngoài
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + ['provider_user_id']
