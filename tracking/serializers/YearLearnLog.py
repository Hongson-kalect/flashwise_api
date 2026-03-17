from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from tracking.models.YearLearnLog import YearLearnLog

User = get_user_model()

class YearLearnLogSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta:
        model = YearLearnLog
        fields = [
            'id', 'sub_id',
            'user', 'year',
            'words_learned', 'words_relearned',
            'learn_time', 'app_time',
            'xp_earned', 'active_days', 'sessions',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
