from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.Device import Device  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class DeviceSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta(BaseModelSerializer.Meta):
        model = Device
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'device_id',
            'os',
            'app_version',
            'last_seen_at',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + ['last_seen_at']
