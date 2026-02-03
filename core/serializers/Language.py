from config.serializers.BaseModel import BaseModelSerializer
from core.models.Language import Language


class LanguageSerializer(BaseModelSerializer):
    class Meta:
        model = Language
        fields = "__all__"

    # def to_representation(self, instance):
    #     data = super().to_representation(instance)
    #     data["direction"] = dict(Language.DIRECTION_CHOICES)[data["direction"]]
    #     return data

