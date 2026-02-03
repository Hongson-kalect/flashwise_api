from rest_framework import serializers
from cache.models.MostLikeWord import MostLikeWord  # hoặc đường dẫn đúng với project của bạn
from core.models import Language, Word  # nếu bạn có model Language và Word

class MostLikeWordSerializer(serializers.ModelSerializer):
    language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all())
    word = serializers.PrimaryKeyRelatedField(queryset=Word.objects.all())

    class Meta:
        model = MostLikeWord
        fields = [
            'language',
            'word',
            'word_sub_id',
            'like_count',
            'total',
            'cached_at',
        ]
        read_only_fields = ['cached_at']
