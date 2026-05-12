from rest_framework import serializers

from ai.models.AISense import AISense
from ai.serializers.AISenseContent import AISenseContentSerializer
from ai.serializers.AISenseMetadata import AISenseMetadataSerializer
from config.serializers.BaseModel import BaseModelSerializer



class AISenseSerializer(serializers.ModelSerializer):
    # Dùng Serializer con để render danh sách content
    # Chúng ta để read_only=True vì dữ liệu đã được nạp sẵn ở View
    metadata = AISenseMetadataSerializer(read_only=True) # Metadata OneToOne
    # contents = AISenseContentSerializer(many=True, read_only=True)
    # definition = serializers.SerializerMethodField()
    # usage = serializers.SerializerMethodField()
    # examples = serializers.SerializerMethodField()
    # translations = serializers.SerializerMethodField()

    class Meta:
        model = AISense
        fields = ['id', 'metadata', 'contents', 'delta','created_at','is_frozen','is_offensive','pos','level','register','ipas',
                  ]

    # def get_definition(self, obj):
    #     return getattr(obj, 'processed_definition', {})

    # def get_usage(self, obj):
    #     return getattr(obj, 'processed_usage', {})

    # def get_examples(self, obj):
    #     return getattr(obj, 'processed_examples', [])

    # def get_translations(self, obj):
    #     return getattr(obj, 'processed_translations', [])
