from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from core.models.Word import Word
from interact.models.LikeWord import LikeWord

User = get_user_model()

class LikeWordSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    word = serializers.PrimaryKeyRelatedField(queryset=Word.objects.all())

    class Meta:
        model = LikeWord
        fields = [
            'id', 'sub_id',
            'user', 'word', 'word_sub_id',
            'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def validate(self, data):
        user = data.get('user')
        word_sub_id = data.get('word_sub_id')
        if LikeWord.objects.filter(user=user, word_sub_id=word_sub_id).exists():
            raise serializers.ValidationError("User has already liked this word.")
        return data
