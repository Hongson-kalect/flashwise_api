from rest_framework import serializers

from ai.models.AISenseContent import AISenseContent
from config.serializers.BaseModel import BaseModelSerializer

class AISenseContentSerializer(serializers.ModelSerializer):
    class Meta:
        model = AISenseContent
        fields = ['id', 'value', 'type', 'language_code', 'parent']
        # fields = '__all__' # Hoặc liệt kê các trường: ['id', 'value', 'type', 'language_code', 'parent']
