from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from tracking.models.DayLearnLog import DayLearnLog

User = get_user_model()

class DayLearnLogSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta:
        model = DayLearnLog
        fields = [
            'id', 'sub_id',
            'user', 'date',
            'words_learned', 'words_relearned',
            'learn_time', 'app_time',
            'xp_earned', 'sessions',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
