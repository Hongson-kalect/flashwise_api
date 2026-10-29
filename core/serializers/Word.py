from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from core.models import Language, WordInfo
from django.contrib.auth import get_user_model

from core.models.Word import Word
from core.serializers.WordForm import BasicFormSerializer
from core.serializers.Language import LanguageSerializer
from core.serializers.WordInfo import BasicWordInfoSerializer, WordInfoSerializer
from utils.models import Ruby
from utils.serializers.Ruby import RubySerializer

User = get_user_model()

class WordSerializer(BaseModelSerializer):
    # language = LanguageSerializer(read_only=True)
    word_info = BasicWordInfoSerializer(read_only=True)
    ruby = serializers.SerializerMethodField()
    # forms = BasicFormSerializer(many=True, read_only=True, source ="word_forms")

    class Meta:
        model = Word
        # fields = "__all__"
        fields = ['id', 'value', 'reading', 'roman', 'language_code', 'word_info', 'ruby', 'synonyms', 'antonyms', 'relateds']
        read_only_fields = ['id', 'created_at', 'updated_at']

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
