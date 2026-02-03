from rest_framework import serializers
from django.contrib.auth import get_user_model

User = get_user_model()

class BaseModelSerializer(serializers.ModelSerializer):
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    deleted_at = serializers.DateTimeField(read_only=True)
    is_deleted = serializers.BooleanField(read_only=True)
    is_active = serializers.BooleanField(default=True)

    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        abstract = True
        fields = [
            'id',
            'is_deleted',
            # 'is_active',
            # 'created_at',
            # 'updated_at',
            # 'deleted_at',
            # 'created_by',
            # 'updated_by',
        ]
