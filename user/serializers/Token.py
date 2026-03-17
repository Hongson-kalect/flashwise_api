from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.Token import Token  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class TokenSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    token_type_display = serializers.CharField(source='get_token_type_display', read_only=True)
    token_value = serializers.CharField(write_only=True)

    class Meta(BaseModelSerializer.Meta):
        model = Token
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'token_type',
            'token_type_display',
            'token_value',
            'expired_at',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields
