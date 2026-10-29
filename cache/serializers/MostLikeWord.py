from rest_framework import serializers
from cache.models.MostLikeWord import MostLikeWord  # hoặc đường dẫn đúng với project của bạn
from core.models import Language
from ai.models import AIWord

class MostLikeWordSerializer(serializers.ModelSerializer):
    language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all())
    word = serializers.PrimaryKeyRelatedField(queryset=AIWord.objects.all())

    class Meta:
        model = MostLikeWord
        fields = [
            'language',
            'word',
            'like_count',
            'total',
            'cached_at',
        ]
        read_only_fields = ['cached_at']
