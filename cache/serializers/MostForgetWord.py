from rest_framework import serializers
from cache.models.MostForgetWord import MostForgetWords  # hoặc đường dẫn đúng với project của bạn
from core.models import Language, Word  # nếu bạn có model Language và Word

class MostForgetWordSerializer(serializers.ModelSerializer):
    language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all())
    word = serializers.PrimaryKeyRelatedField(queryset=Word.objects.all())

    class Meta:
        model = MostForgetWords
        fields = [
            'language',
            'word',
            'word_sub_id',
            'forgot_count',
            'total_count',
            'cached_at',
        ]
        read_only_fields = ['cached_at']
