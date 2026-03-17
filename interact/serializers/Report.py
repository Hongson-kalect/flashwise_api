from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from interact.models.Report import Report

User = get_user_model()

class ReportSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    type_display = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    target_type_display = serializers.SerializerMethodField()

    class Meta:
        model = Report
        fields = [
            'id', 'sub_id',
            'user', 'target_id', 'target_type', 'target_type_display',
            'type', 'type_display',
            'status', 'status_display',
            'reason', 'message', 'result',
            'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_type_display(self, obj):
        return dict(Report.TYPE_CHOICES).get(obj.type)

    def get_status_display(self, obj):
        return dict(Report.STATUS_CHOICES).get(obj.status)

    def get_target_type_display(self, obj):
        return dict(Report.TARGET_CHOICES).get(obj.target_type)
