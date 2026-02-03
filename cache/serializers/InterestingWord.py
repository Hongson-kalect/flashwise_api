from rest_framework import serializers
from cache.models.InterestingWord import InterestingWord  # hoặc đường dẫn đúng với project của bạn
from core.models import Language, Word  # nếu bạn có model Language và Word

class InterestingWordsSerializer(serializers.ModelSerializer):
    language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all())
    word = serializers.PrimaryKeyRelatedField(queryset=Word.objects.all())

    class Meta:
        model = InterestingWord
        fields = [
            'language',
            'word',
            'word_sub_id',
            'view_count',
            'total',
            'last_view_at',
        ]
        read_only_fields = ['last_view_at']
