from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from core.models.CommonPhrase import CommonPhrase
from utils.models import Tag
from core.models import Language

class CommonPhraseSerializer(BaseModelSerializer):
    tag = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Tag.objects.all(), required=False
    )
    language = serializers.PrimaryKeyRelatedField(
        queryset=Language.objects.all(), required=False, allow_null=True
    )
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = CommonPhrase
        fields = [
            'id', 'sub_id', 'phrase', 'description', 'image', 'image_url',
            'tag', 'language', 'is_deleted',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None

    def create(self, validated_data):
        tag_names = validated_data.pop('tags', [])
        tags = [Tag.objects.get_or_create(name=name.strip())[0] for name in tag_names]

        instance = CommonPhrase.objects.create(**validated_data)

        instance.tag.set(tags)
        return instance

    def update(self, instance, validated_data):
        tag_names = validated_data.pop('tag', None)
        tags = [Tag.objects.get_or_create(name=name.strip())[0] for name in tag_names]

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if tags is not None:
            instance.tag.set(tags)
        return instance
