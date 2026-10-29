from rest_framework import serializers
from cache.models.MostForgetWord import MostForgetWord
from core.models import Language
from ai.models import AISense

class MostForgetWordSerializer(serializers.ModelSerializer):
    language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all())
    sense = serializers.PrimaryKeyRelatedField(queryset=AISense.objects.all())

    class Meta:
        model = MostForgetWord
        fields = [
            'language',
            'sense',
            'forgot_count',
            'total_count',
            'cached_at',
        ]
        read_only_fields = ['cached_at']
