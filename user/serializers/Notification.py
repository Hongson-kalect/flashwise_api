from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.Notification import Notification  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class NotificationSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta(BaseModelSerializer.Meta):
        model = Notification
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'type',
            'title',
            'content',
            'is_read',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields
