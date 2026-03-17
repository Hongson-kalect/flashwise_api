from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from tracking.models.LearnLog import LearnLog

User = get_user_model()

class LearnLogSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    result_display = serializers.SerializerMethodField()

    class Meta:
        model = LearnLog
        fields = [
            'id', 'sub_id',
            'user', 'session_id',
            'learned_at', 'result', 'result_display',
            'xp_earned',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_result_display(self, obj):
        return dict(LearnLog.RESULT_CHOICES).get(obj.result)
