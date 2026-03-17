from config.serializers.BaseModel import BaseModelSerializer
from rest_framework import serializers
from user.models.Asset import Asset
from django.contrib.auth import get_user_model

User = get_user_model()


class AssetSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta(BaseModelSerializer.Meta):
        model = Asset
        fields = BaseModelSerializer.Meta.fields + ['user', 'asset_type', 'asset_id']
