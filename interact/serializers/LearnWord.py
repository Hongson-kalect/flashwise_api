from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from django.contrib.auth import get_user_model

from core.models.Word import Word
from interact.models.LearnWord import LearnWord

User = get_user_model()

class LearnWordSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    word = serializers.PrimaryKeyRelatedField(queryset=Word.objects.all())

    class Meta:
        model = LearnWord
        fields = [
            'id', 'sub_id',
            'user', 'word', 'word_sub_id',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
