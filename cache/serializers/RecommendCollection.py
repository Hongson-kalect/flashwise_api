from rest_framework import serializers
from cache.models.RecommendCollection import RecommendCollection  # hoặc đường dẫn đúng với project của bạn
from core.models import Language, Collection  # nếu bạn có model Language và Collection

class RecommendCollectionSerializer(serializers.ModelSerializer):
    language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all())
    collection = serializers.PrimaryKeyRelatedField(queryset=Collection.objects.all())
    strategy_display = serializers.CharField(source='get_strategy_display', read_only=True)

    class Meta:
        model = RecommendCollection
        fields = [
            'language',
            'collection',
            'collection_sub_id',
            'strategy',
            'strategy_display',
            'generated_at',
        ]
        read_only_fields = ['generated_at']
