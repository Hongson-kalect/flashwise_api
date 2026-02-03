from rest_framework import serializers

from config.models.UUIDModel import UUIDModel

class UUIDModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = UUIDModel
        fields = ['id']
