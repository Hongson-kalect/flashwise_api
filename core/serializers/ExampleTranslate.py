from rest_framework import serializers
from core.models.ExampleTranslate import ExampleTranslate

class ExampleTranslateSerializer(serializers.ModelSerializer):
    lang = serializers.StringRelatedField()  # hiển thị __str__ của Language

    class Meta:
        model = ExampleTranslate
        fields = "__all__"

class BasicExampleTranslateSerializer(serializers.ModelSerializer):

    class Meta:
        model = ExampleTranslate
        fields = [
            # 'id',
            # 'sub_id',
            'value',
            # 'language_code',
            # 'translate',
            # "example",
        ]
