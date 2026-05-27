from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from ai.models.AISense import AISense
from ai.serializers.AISense import AISenseSerializer, AISenseSerializerBasic
from core.models.CollectionItem import CollectionItem
from utils.models.Tag import Tag

from core.models.CollectionItem import CollectionItem

class CollectionItemSerializer(BaseModelSerializer):

    sense = AISenseSerializer(read_only=True)

    class Meta:
        model = CollectionItem
        fields = ['id', 'original_id', 'sense', 'order']

class CollectionItemBasicSerializer(BaseModelSerializer):

    sense = AISenseSerializerBasic(read_only=True)

    class Meta:
        model = CollectionItem
        fields = ['id', 'original_id', 'sense', 'order']