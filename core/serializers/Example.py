from rest_framework import serializers
from core.models.Example import Example
from core.serializers.ExampleTranslate import BasicExampleTranslateSerializer

class ExampleSerializer(serializers.ModelSerializer):

    class Meta:
        model = Example
        fields = "__all__"

class BasicExampleSerializer(serializers.ModelSerializer):
    example_translate = BasicExampleTranslateSerializer(many=True, read_only=True, source='translated_examples')

    class Meta:
        model = Example
        fields = [
            # 'id',
            # 'sub_id',
            'value',
            # 'language_code',
            # 'bold',
            # "score",
            # 'defination',
            'example_translate'
        ]
