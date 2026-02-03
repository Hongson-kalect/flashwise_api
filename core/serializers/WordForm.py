from rest_framework import serializers
from core.models.WordForm import WordForm

class FormSerializer(serializers.ModelSerializer):
    class Meta:
        model = WordForm
        fields = "__all__"
class BasicFormSerializer(serializers.ModelSerializer):
    class Meta:
        model = WordForm
        fields = ["value", "type"]