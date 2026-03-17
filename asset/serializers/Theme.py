from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from asset.models.Theme import Theme  # hoặc đường dẫn đúng với project của bạn

class ThemeSerializer(BaseModelSerializer):
    class Meta(BaseModelSerializer.Meta):
        model = Theme
        fields = BaseModelSerializer.Meta.fields + [
            'name',
            'color_palette',
            'font',
            'is_default',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields
