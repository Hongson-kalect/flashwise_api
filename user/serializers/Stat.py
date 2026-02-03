from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.Stat import Stat  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class StatSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta(BaseModelSerializer.Meta):
        model = Stat
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'total_words_learned',
            'total_sessions',
            'total_app_time',
            'current_streak',
            'max_streak',
            'total_xp',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields
