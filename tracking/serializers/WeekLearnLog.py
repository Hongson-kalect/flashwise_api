from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from tracking.models.WeekLearnLog import WeekLearnLog

User = get_user_model()

class WeekLearnLogSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta:
        model = WeekLearnLog
        fields = [
            'id', 'sub_id',
            'user', 'week_start_date',
            'words_learned', 'words_relearned',
            'learn_time', 'app_time',
            'xp_earned', 'sessions', 'active_days',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
