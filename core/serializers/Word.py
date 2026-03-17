from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from core.models import Language, Translate, WordInfo
from django.contrib.auth import get_user_model

from core.models.Word import Word
from core.serializers.WordForm import BasicFormSerializer
from core.serializers.Defination import DefinationSerializer
from core.serializers.Example import BasicExampleSerializer, ExampleSerializer
from core.serializers.ExampleTranslate import BasicExampleTranslateSerializer
from core.serializers.Language import LanguageSerializer
from core.serializers.Translate import BasicTranslateSerializer, TranslateSerializer
from core.serializers.WordInfo import BasicWordInfoSerializer, WordInfoSerializer
from utils.models import Ruby
from utils.serializers.Ruby import RubySerializer

User = get_user_model()

class WordSerializer(BaseModelSerializer):
    # language = LanguageSerializer(read_only=True)
    word_info = BasicWordInfoSerializer(read_only=True)
    ruby = serializers.SerializerMethodField()
    translates = BasicTranslateSerializer(many=True, read_only=True, source="word_translates")
    definations = DefinationSerializer(many=True, read_only=True)
    # forms = BasicFormSerializer(many=True, read_only=True, source ="word_forms")

    class Meta:
        model = Word
        # fields = "__all__"
        fields = ['value', 'language_code','word_info','ruby', 'translates', 'definations']
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None

    # def get_audio_url(self, obj):
    #     request = self.context.get('request')
    #     if obj.audio and request:
    #         return request.build_absolute_uri(obj.audio.url)
    #     return None

    def get_ruby(self, obj):
        """
        obj.rubys = [uuid7, uuid8, ...]
        lấy danh sách Ruby tương ứng
        """
        
        ruby_ids = obj.rubys or []   # JSON list
        rubys = Ruby.objects.filter(id__in=ruby_ids)

        return RubySerializer(rubys, many=True).data
