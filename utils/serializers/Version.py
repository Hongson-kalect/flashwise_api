from rest_framework import serializers
from django.contrib.auth import get_user_model

User = get_user_model()

from config.serializers.BaseModel import BaseModelSerializer
from utils.models.Version import Version

class VersionSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    entity_type_display = serializers.SerializerMethodField()

    class Meta:
        model = Version
        fields = [
            'id', 'sub_id',
            'user', 'entityId', 'entityType', 'entity_type_display',
            'version', 'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_entity_type_display(self, obj):
        return dict(Version._meta.get_field('entityType').choices).get(obj.entityType)
