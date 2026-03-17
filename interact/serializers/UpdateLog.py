from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from interact.models.UpdateLog import UpdateLog

User = get_user_model()

class UpdateLogSerializer(BaseModelSerializer):
    request_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True)
    type_display = serializers.SerializerMethodField()

    class Meta:
        model = UpdateLog
        fields = [
            'id', 'sub_id',
            'type', 'type_display',
            'target_id', 'value',
            'request_by',
            'change_from', 'change_to',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_type_display(self, obj):
        return dict(UpdateLog._meta.get_field('type').choices).get(obj.type)
