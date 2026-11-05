from rest_framework import serializers

from ai.models.AISense import AISense
from ai.serializers.AISenseContent import AISenseContentSerializer
from ai.serializers.AISenseMetadata import AISenseMetadataSerializer
from config.serializers.BaseModel import BaseModelSerializer



class AISenseSerializer(serializers.ModelSerializer):
    # Dùng Serializer con để render danh sách content
    metadata = AISenseMetadataSerializer(read_only=True) # Metadata OneToOne
    recommended = serializers.SerializerMethodField()  # Admin / seeder đề xuất nghĩa này (app hiện huy hiệu)

    class Meta:
        model = AISense
        fields = ['id', 'word_value', 'metadata', 'contents', 'delta','created_at','is_frozen','is_offensive','pos','level','register','ipas','audio','image_preview','is_official','original','quality_status','ai_pass','voted','devoted','like_count','score','recommended','global_difficulty','learner_count']

    def get_recommended(self, obj):
        return obj.recommended_at is not None

class AISenseSerializerBasic(serializers.ModelSerializer):

    class Meta:
        model = AISense
        fields = ['id', "word_value", 'preview', 'created_at','updated_at', 'is_offensive', 'pos', 'level','register','ipas','audio','quality_status','global_difficulty','learner_count']
