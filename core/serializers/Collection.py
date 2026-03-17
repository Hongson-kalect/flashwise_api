from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from core.models.Word import Word
from core.models.Collection import Collection
from utils.models.Tag import Tag

class CollectionSerializer(BaseModelSerializer):
    tag = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Tag.objects.all(), required=False
    )
    words = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Word.objects.all(), required=False
    )
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = [
            'id', 'sub_id', 'name', 'description', 'image', 'image_url',
            'words', 'tag', 'is_deleted', 'is_active',
            'update_requests', 'update_logs',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None

    def create(self, validated_data):
        words = validated_data.pop('words', [])
        tag_names = validated_data.pop('tags', [])
        
        collection = Collection.objects.create(**validated_data)

        tags = [Tag.objects.get_or_create(name=name.strip())[0] for name in tag_names]

        collection.tag.set(tags)
        collection.words.set(words)
        return collection

    def update(self, instance, validated_data):
        words = validated_data.pop('words', None)
        tag_names = validated_data.pop('tags', [])
        tags = [Tag.objects.get_or_create(name=name.strip())[0] for name in tag_names]

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if words is not None:
            instance.words.set(words)
        if tags is not None:
            instance.tags.set(tags)

        return instance
