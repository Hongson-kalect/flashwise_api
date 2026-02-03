from rest_framework import serializers

from config.serializers.BaseModel import BaseModelSerializer
from utils.models.Snapshot import SnapShot
class SnapShotSerializer(BaseModelSerializer):
    class Meta:
        model = SnapShot
        fields = [
            'id', 'sub_id',
            'type', 'target_id', 'snap',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']
