from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.LoginProvider import LoginProvider  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class LoginProviderSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta(BaseModelSerializer.Meta):
        model = LoginProvider
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'provider',
            'avatar_url',
            'url',
            'bio',
            'gender',
            'dob',
            'token',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields
