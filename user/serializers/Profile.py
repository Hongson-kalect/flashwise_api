from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.UserProfile import UserProfile  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class ProfileSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    gender_display = serializers.CharField(source='get_gender_display', read_only=True)
    tier_display = serializers.CharField(source='get_tier_display', read_only=True)
    provider_display = serializers.CharField(source='get_provider_display', read_only=True)


    class Meta(BaseModelSerializer.Meta):
        model = UserProfile
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'full_name',
            'avatar_url',
            'age',
            'gender',
            'native_language',
            'dob',
            'country',
            'learning_languages',
            'join_date',
            'login_id',
            'time_zone',
            'zone_num',
            'is_guest',
            'tier',
            'tier_expired_at',
            'provider',
            'provider_user_id',
            'fav_time',
            'level',
            'birth_date',
            'is_active',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + ['join_date']
