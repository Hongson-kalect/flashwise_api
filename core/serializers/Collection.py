from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from ai.serializers.AISense import AISenseSerializerBasic
from core.models.Collection import Collection
from utils.models.Tag import Tag

LIST_FIELDS = ['id', 'name', 'description', 'image_url', 'cover_image', 'cover_word', 'senses_count', 'is_official', 'language_code', 'like_count',
               'downloaded_count', 'view_count', 'tags', 'created_at', 'has_offensive']
BASIC_FIELDS = ['id', 'name', 'description', 'image_url', 'is_official', 'item_count', 'language_code', 'version']


# Dùng riêng cho hàm list (Cực nhẹ)
class CollectionListSerializer(BaseModelSerializer):
    senses_count = serializers.IntegerField(read_only=True) # Nhận từ annotate
    has_offensive = serializers.BooleanField(read_only=True, default=False)  # Nhận từ annotate: bộ có nghĩa thô tục
    # Ảnh bìa khi tác giả chưa đặt ảnh riêng: ảnh của nghĩa đầu tiên (theo thứ tự trong bộ) có ảnh; cover_word là từ của nghĩa đầu
    # để app tìm ảnh mở khi không nghĩa nào có ảnh. Cả trang được tính bằng 1 truy vấn (cache trên serializer con của ListSerializer).
    cover_image = serializers.SerializerMethodField()
    cover_word = serializers.SerializerMethodField()

    def _covers(self, obj):
        cache = getattr(self, '_cover_cache', None)
        if cache is None or obj.pk not in cache:
            from core.models import CollectionItem
            parent = getattr(self, 'parent', None)
            instance = getattr(parent, 'instance', None) if parent is not None else None
            objs = [c for c in instance] if instance is not None and not hasattr(instance, 'pk') else [obj]
            ids = [c.pk for c in objs]
            versions = {c.pk: c.version or 0 for c in objs}
            # Dòng đang hiệu lực của từng bộ: mỗi từ (original_id) lấy bản phát hành mới nhất tới version hiện hành, bỏ dòng xoá (như current_items)
            rows = CollectionItem.objects.filter(collection_id__in=ids, released=True)                 .order_by('collection_id', 'original_id', '-version', '-created_at')                 .values_list('collection_id', 'original_id', 'version', 'is_deleted', 'display_order', 'created_at', 'sense__image_preview', 'sense__word_value')
            latest = {}
            for collection_id, original_id, version, deleted, order, created, image, word in rows:
                if (version or 0) > versions[collection_id] or (collection_id, original_id) in latest:
                    continue
                latest[(collection_id, original_id)] = (deleted, order or 0, created, image, word)
            live = {pk: [] for pk in ids}
            for (collection_id, _), (deleted, order, created, image, word) in latest.items():
                if not deleted:
                    live[collection_id].append((order, created, image, word))
            cache = {}
            for pk, items in live.items():
                items.sort(key=lambda x: (x[0], x[1]))
                cache[pk] = {'word': items[0][3] if items else None, 'image': next((i[2] for i in items if i[2]), None)}
            self._cover_cache = cache
        return cache.get(obj.pk) or {'image': None, 'word': None}

    def get_cover_image(self, obj):
        return self._covers(obj)['image']

    def get_cover_word(self, obj):
        return self._covers(obj)['word']

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
