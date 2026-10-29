from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.UserSetting import UserSetting

User = get_user_model()

class UserSettingSerializer(BaseModelSerializer):
    # Cài đặt dạng key - value, danh sách key mặc định xem trong user/models/UserSetting.py
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta(BaseModelSerializer.Meta):
        model = UserSetting
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'key',
            'value',
            'updated_at',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields
