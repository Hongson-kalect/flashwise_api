from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from core.models import Word
from django.contrib.auth import get_user_model

from interact.models.ForgetWord import ForgetWord

User = get_user_model()

class ForgetWordSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    word = serializers.PrimaryKeyRelatedField(queryset=Word.objects.all())

    class Meta:
        model = ForgetWord
        fields = [
            'id', 'sub_id',
            'user', 'word', 'word_sub_id',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
