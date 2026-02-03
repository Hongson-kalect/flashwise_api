from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from interact.models.WordStatus import WordStatus

User = get_user_model()

class WordStatusSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    status_display = serializers.SerializerMethodField()

    class Meta:
        model = WordStatus
        fields = [
            'id', 'sub_id',
            'user', 'word_sub_id',
            'status', 'status_display',
            'level', 'is_mastered', 'is_hidden', 'is_avoid',
            'reason', 'last_seen_at', 'last_learn_at',
            'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_status_display(self, obj):
        return dict(WordStatus.STATUS_CHOICES).get(obj.status)
