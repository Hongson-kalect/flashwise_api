from rest_framework import serializers
from cache.models.MostLikeCollection import MostLikeCollection  # hoặc đường dẫn đúng với project của bạn
from core.models import Language, Collection  # nếu bạn có model Language và Word

class MostLikeCollectionSerializer(serializers.ModelSerializer):
    language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all())
    collection = serializers.PrimaryKeyRelatedField(queryset=Collection.objects.all())

    class Meta:
        model = MostLikeCollection
        fields = [
            'language',
            'collection',
            'collection_sub_id',
            'like_count',
            'total',
            'cached_at',
        ]
        read_only_fields = ['cached_at']
