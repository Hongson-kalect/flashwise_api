from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.UserProfile import UserProfile

User = get_user_model()

class ProfileSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    gender_display = serializers.CharField(source='get_gender_display', read_only=True)
    tier_display = serializers.CharField(source='get_tier_display', read_only=True)
    is_active = serializers.BooleanField(read_only=True) # Không để client tự mở khoá tài khoản bị chặn


    class Meta(BaseModelSerializer.Meta):
        model = UserProfile
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'full_name',
            'avatar_url',
            'age',
            'gender',
            'gender_display',
            'dob',
            'country_code',
            'native_language',
            'learning_languages',
            'fav_time',
            'level',
            'total_xp',
            'current_streak',
            'max_streak',
            'tier',
            'tier_display',
            'tier_expired_at',
            'time_zone',
            'zone_num',
            'is_active',
            'created_at',
        ]
        # Các trường do server tính, client không được tự ghi
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + [
            'level', 'total_xp', 'current_streak', 'max_streak', 'tier', 'tier_expired_at', 'is_active',
        ]
