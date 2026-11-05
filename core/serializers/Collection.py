from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from ai.serializers.AISense import AISenseSerializerBasic
from core.models.Collection import Collection
from utils.models.Tag import Tag

LIST_FIELDS = ['id', 'name', 'description', 'image_url', 'senses_count', 'is_official', 'language_code', 'like_count', 'downloaded_count',
               'view_count', 'tags', 'created_at']
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
    translation = serializers.SerializerMethodField()
    author_id = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = ['id', 'name', 'description', 'image_url', 'is_official', 'item_count', 'language_code', 'version', 'senses', 'translation', 'invalid_words', 'pending_words', 'author_id', 'like_count',
                  'view_count', 'downloaded_count', 'tags']
        read_only_fields = fields

    def get_translation(self, obj):
        """Bộ đã dịch được bao nhiêu nghĩa sang ngôn ngữ người xem (query user_lang): {language, total, missing}. Không có user_lang: null."""
        from ai.translation import collect_gaps, missing_sense_ids, queue_gaps

        request = self.context.get('request')
        lang = request.query_params.get('user_lang') if request is not None else None
        items = self.context.get('items')
        if not lang or items is None:
            return None
        senses = [item.sense for item in items]
        gaps = collect_gaps(senses, lang)
        if gaps:
            queue_gaps(gaps, lang, lazy=True)  # có người xem bộ: dịch dần ở nền nếu cặp ngôn ngữ phổ biến
        return {'language': lang, 'total': len(senses), 'missing': len(missing_sense_ids(gaps))}

    def get_author_id(self, obj):
        # Id hồ sơ (UserProfile) của tác giả, dùng để report user; bộ chính thức không cần
        if obj.is_official or not obj.created_by_id:
            return None
        from user.models import UserProfile
        profile_id = UserProfile.objects.filter(user_id=obj.created_by_id).values_list('id', flat=True).first()
        return str(profile_id) if profile_id else None

    def get_senses(self, obj):
        # Theo thứ tự người dùng sắp (display_order), bỏ nghĩa đã xoá khỏi bộ. Truyền sẵn items qua context để khỏi truy vấn lại
        items = self.context.get('items')
        if items is None:
            items = obj.collectionitem_set.exclude(is_deleted=True).select_related('sense').order_by('display_order', 'created_at')
        senses = [item.sense for item in items]
        data = AISenseSerializerBasic(senses, many=True).data
        request = self.context.get('request')
        lang = request.query_params.get('user_lang') if request is not None else None
        # gloss: nghĩa ngắn theo ngôn ngữ người xem (bản dịch, không có thì định nghĩa), để xem trước bộ không cần tải
        for row, sense in zip(data, senses):
            contents = sense.contents if isinstance(sense.contents, dict) else {}
            translations = (contents.get('translations') or {}).get(lang) if lang else None
            definition = ((contents.get('definition') or {}).get(lang) or (contents.get('definition') or {}).get(sense.language_code) or {})
            row['gloss'] = ', '.join(translations) if translations else (definition.get('value') if isinstance(definition, dict) else '') or ''
            row['image_preview'] = sense.image_preview
        return data


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
