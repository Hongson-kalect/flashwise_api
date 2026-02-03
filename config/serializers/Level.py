from rest_framework import serializers

from config.models.Level import Level
from config.serializers.BaseModel import BaseModelSerializer

class LevelSerializer(BaseModelSerializer):
    class Meta:
        model = Level
        fields = "__all__"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["name"] = str(instance)
        return data
