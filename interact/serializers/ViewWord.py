from rest_framework import serializers
from django.contrib.auth import get_user_model

from config.serializers.BaseModel import BaseModelSerializer
from core.models.Word import Word
from interact.models.ViewWord import ViewWord

User = get_user_model()

class ViewWordSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    word = serializers.PrimaryKeyRelatedField(queryset=Word.objects.all(), allow_null=True)

    class Meta:
        model = ViewWord
        fields = [
            'id', 'sub_id',
            'user', 'word', 'word_sub_id',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def validate(self, data):
        user = data.get('user')
        word = data.get('word')
        if word and ViewWord.objects.filter(user=user, word=word).exists():
            raise serializers.ValidationError("User has already viewed this word.")
        return data
