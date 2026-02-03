
from config.serializers.BaseModel import BaseModelSerializer
from utils.models.Ruby import Ruby

class RubySerializer(BaseModelSerializer):

    class Meta:
        model = Ruby
        fields = [
            "value",
            'on_yomi',
            'kun_yomi',
            'type',
            'meanings',
        ]
