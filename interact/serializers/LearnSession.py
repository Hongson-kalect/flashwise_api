from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from interact.models.LearnSession import LearnSession

User = get_user_model()

class LearnSessionSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    type_display = serializers.SerializerMethodField()

    class Meta:
        model = LearnSession
        fields = [
            'id', 'sub_id',
            'user', 'start_at', 'end_at',
            'words', 'word_count', 'time',
            'type', 'type_display',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_type_display(self, obj):
        return dict(LearnSession.TYPE_CHOICES).get(obj.type)
