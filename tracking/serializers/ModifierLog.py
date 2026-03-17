from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from tracking.models.ModifierLog import ModifierLog

User = get_user_model()

class ModifierLogSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    action_display = serializers.SerializerMethodField()

    class Meta:
        model = ModifierLog
        fields = [
            'id', 'sub_id',
            'user', 'target_type', 'target_id',
            'action', 'action_display',
            'meta', 'is_success',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_action_display(self, obj):
        return dict(ModifierLog.ACTION_CHOICES).get(obj.action)
