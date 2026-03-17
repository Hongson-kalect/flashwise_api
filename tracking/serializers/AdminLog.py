from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from tracking.models.AdminLog import AdminLog

User = get_user_model()

class AdminLogSerializer(BaseModelSerializer):
    admin = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True)
    action_display = serializers.SerializerMethodField()

    class Meta:
        model = AdminLog
        fields = [
            'id', 'sub_id',
            'admin', 'action', 'action_display',
            'target_id', 'target_type',
            'reason', 'meta', 'is_success',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_action_display(self, obj):
        return dict(AdminLog.ACTION_CHOICES).get(obj.action)
