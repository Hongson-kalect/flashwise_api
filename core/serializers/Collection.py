from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from ai.serializers.AISense import AISenseSerializerBasic
from core.models.Collection import Collection
from utils.models.Tag import Tag

LIST_FIELDS = ['id', 'name', 'description', 'image_url', 'senses_count', 'is_official', 'language_code', 'like_count', 'downloaded_count']
BASIC_FIELDS = ['id', 'name', 'description', 'image_url', 'is_official', 'item_count', 'language_code', 'version']


# Dùng riêng cho hàm list (Cực nhẹ)
class CollectionListSerializer(BaseModelSerializer):
    senses_count = serializers.IntegerField(read_only=True) # Nhận từ annotate

    class Meta:
        model = Collection
        fields = LIST_FIELDS
        read_only_fields = LIST_FIELDS


class CollectionBasicSerializer(BaseModelSerializer):
    class Meta:
        model = Collection
        fields = BASIC_FIELDS
        read_only_fields = BASIC_FIELDS


# Dùng riêng cho hàm retrieve (Chi tiết kèm từ vựng)
class CollectionDetailSerializer(BaseModelSerializer):
    senses = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = ['id', 'name', 'description', 'image_url', 'is_official', 'item_count', 'language_code', 'version', 'senses', 'invalid_words', 'pending_words']
        read_only_fields = fields

    def get_senses(self, obj):
        # Theo thứ tự người dùng sắp (display_order), bỏ nghĩa đã xoá khỏi bộ. Truyền sẵn items qua context để khỏi truy vấn lại
        items = self.context.get('items')
        if items is None:
            items = obj.collectionitem_set.exclude(is_deleted=True).select_related('sense').order_by('display_order', 'created_at')
        return AISenseSerializerBasic([item.sense for item in items], many=True).data


class CollectionSerializer(BaseModelSerializer):
    """Ghi (create / update): người dùng chỉ được đặt tên, mô tả, thẻ và ngôn ngữ. Cờ official, bộ đếm, phiên bản... chỉ server / admin đặt."""
    tags = serializers.PrimaryKeyRelatedField(many=True, queryset=Tag.objects.all(), required=False)

    class Meta:
        model = Collection
        fields = [
            'id', 'name', 'description', 'image_url', 'tags', 'language_code',
            'item_count', 'like_count', 'view_count', 'downloaded_count',
            'is_official', 'released', 'version', 'created_at', 'updated_at',
        ]
        read_only_fields = [f for f in fields if f not in ('name', 'description', 'tags', 'language_code')]
