from rest_framework import serializers

from config.serializers.BaseModel import BaseModelSerializer
from interact.models.UserNote import UserNote

class UserNoteSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = UserNote
        fields = [
            'id', 'user', 'sense', 'content', 'version',
            'is_deleted', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
