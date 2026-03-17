from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from interact.models.UpdateRequest import UpdateRequest

User = get_user_model()

class UpdateRequestSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    updated_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True, required=False)
    type_display = serializers.SerializerMethodField()

    class Meta:
        model = UpdateRequest
        fields = [
            'id', 'sub_id',
            'type', 'type_display',
            'target_id', 'value',
            'user', 'updated_by',
            'is_active', 'is_approval', 'approval_at', 'sync',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_type_display(self, obj):
        return dict(UpdateRequest._meta.get_field('type').choices).get(obj.type)
