from rest_framework import serializers
from django.contrib.auth import get_user_model
from user.models.BanList import BanList  # hoặc đường dẫn đúng với project của bạn
from config.serializers.BaseModel import BaseModelSerializer

User = get_user_model()

class BanListSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    banned_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True)

    class Meta(BaseModelSerializer.Meta):
        model = BanList
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'reason',
            'banned_by',
            'start_at',
            'end_at',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + ['start_at']
