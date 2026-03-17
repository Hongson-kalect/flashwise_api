from rest_framework import serializers
from core.models.Defination import Defination
from core.serializers.Example import BasicExampleSerializer
from core.serializers.ExampleTranslate import BasicExampleTranslateSerializer

class DefinationSerializer(serializers.ModelSerializer):
    # lang = serializers.StringRelatedField()  # hiển thị __str__ của Language
    examples = BasicExampleSerializer(many=True, read_only=True, source="defination_examples")    

    class Meta:
        model = Defination
        fields = [
            # "id",
            # "sub_id",
            # 'language',
            # 'language_code',
            # "defination_type",
            "value",
            'examples'
            # "bold",
            # "score",
            # "roman",
            # "ruby",
        ]
