from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.UserSetting import UserSetting
from core.models import Language  # nếu bạn dùng ForeignKey đến Language

User = get_user_model()

class UserSettingSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    app_language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all(), allow_null=True)
    language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all(), allow_null=True)
    learning_language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all(), allow_null=True)

    theme_display = serializers.CharField(source='get_theme_display', read_only=True)
    preferred_learning_mode_display = serializers.CharField(source='get_preferred_learning_mode_display', read_only=True)
    target_type_display = serializers.CharField(source='get_target_type_display', read_only=True)

    class Meta(BaseModelSerializer.Meta):
        model = UserSetting
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'theme',
            'theme_display',
            'app_language',
            'language',
            'learning_language',
            'notification_enabled',
            'vibration',
            'sound',
            'preferred_learning_mode',
            'preferred_learning_mode_display',
            'preferred_learning_time',
            'target_type',
            'target_type_display',
            'target_num',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields
