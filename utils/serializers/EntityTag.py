from rest_framework import serializers

from config.serializers.BaseModel import BaseModelSerializer
from utils.models.EntityTag import EntityTag
from utils.models.Tag import Tag

class EntityTagSerializer(BaseModelSerializer):
    tag = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all())
    entity_type_display = serializers.SerializerMethodField()

    class Meta:
        model = EntityTag
        fields = [
            'id', 'sub_id',
            'tag', 'entityId', 'entityType', 'entity_type_display',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_entity_type_display(self, obj):
        return dict(EntityTag._meta.get_field('entityType').choices).get(obj.entityType)
