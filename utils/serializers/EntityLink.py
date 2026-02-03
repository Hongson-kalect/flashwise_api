from rest_framework import serializers
from django.contrib.auth import get_user_model

User = get_user_model()

from config.serializers.BaseModel import BaseModelSerializer
from utils.models.EntityLink import EntityLink

class EntityLinkSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    entity_type_display = serializers.SerializerMethodField()

    class Meta:
        model = EntityLink
        fields = [
            'id', 'sub_id',
            'user', 'is_active',
            'entityId', 'entityType', 'entity_type_display',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    # def get_entity_type_display(self, obj):
    #     return dict(EntityTag._meta.get_field('entityType').choices).get(obj.entityType)
