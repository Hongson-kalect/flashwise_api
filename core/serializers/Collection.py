from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from ai.serializers.AISense import AISenseSerializer,AISenseSerializerBasic
from core.models.Collection import Collection
from utils.models.Tag import Tag

# Dùng riêng cho hàm list (Cực nhẹ)
class CollectionListSerializer(BaseModelSerializer):
    senses_count = serializers.IntegerField(read_only=True) # Nhận từ annotate

    class Meta:
        model = Collection
        fields = ['id', 'name', 'description', 'image_url', 'senses_count', 'is_official', 'language_code', 'like_count', 'downloaded_count']

# Dùng riêng cho hàm retrieve (Chi tiết kèm từ vựng)
class CollectionBasicSerializer(BaseModelSerializer):
    # Bạn có thể dùng Serializer của CollectionItem ở đây để lôi chi tiết word, meaning...
    # items = serializers.SerializerMethodField() 

    class Meta:
        model = Collection
        fields = ['id', 'name', 'description', 'image_url', 'is_official', 'item_count', 'language_code', 'version']

    # def get_items(self, obj):
    #     # Truy cập vào tập dữ liệu đã được prefetch tối ưu ở hàm retrieve
    #     from .CollectionItem import CollectionItemSerializer # import tại đây tránh vòng lặp
    #     return CollectionItemSerializer(obj.collectionitem_set.all(), many=True).data

class CollectionDetailSerializer(BaseModelSerializer):
    # Bạn có thể dùng Serializer của CollectionItem ở đây để lôi chi tiết word, meaning...
    # items = serializers.SerializerMethodField() 
    senses = AISenseSerializerBasic(many=True, read_only=True)

    class Meta:
        model = Collection
        fields = ['id', 'name', 'description', 'image_url', 'is_official', 'senses', 'invalid_words','pending_words']

    # def get_items(self, obj):
    #     # Truy cập vào tập dữ liệu đã được prefetch tối ưu ở hàm retrieve
    #     from .CollectionItem import CollectionItemSerializer # import tại đây tránh vòng lặp
    #     return CollectionItemSerializer(obj.collectionitem_set.all(), many=True).data

class CollectionSerializer(BaseModelSerializer):
    tags = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Tag.objects.all(), required=False
    )
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = [
            'id', 'name', 'description', 'image', 'image_url', 'tags',
            'language_code', 'item_count', 'score',
            'like_count', 'view_count', 'downloaded_count',
            'is_official', 'is_frozen', 'released', 'version',
            'invalid_words', 'pending_words', 'original', 'previous', 'parents',
            'is_deleted', 'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'item_count', 'score', 'like_count', 'view_count', 'downloaded_count',
            'created_at', 'updated_at',
        ]

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return obj.image_url
