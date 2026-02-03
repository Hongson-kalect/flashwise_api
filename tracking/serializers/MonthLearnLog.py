from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from tracking.models.MonthLearnLog import MonthLearnLog

User = get_user_model()

class MonthLearnLogSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta:
        model = MonthLearnLog
        fields = [
            'id', 'sub_id',
            'user', 'month_start_date',
            'words_learned', 'words_relearned',
            'learn_time', 'app_time',
            'xp_earned', 'active_days', 'sessions',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
