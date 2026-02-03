from rest_framework import serializers

from config.serializers.BaseModel import BaseModelSerializer
from core.models.Quiz import Quiz

class QuizSerializer(BaseModelSerializer):
    level_display = serializers.SerializerMethodField()
    question_type_display = serializers.SerializerMethodField()
    answer_type_display = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            'id', 'sub_id', 'level', 'level_display',
            'question_type', 'question_type_display',
            'question', 'options', 'answer',
            'answer_type', 'answer_type_display',
            'explanation', 'word',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_level_display(self, obj):
        return dict(Quiz.LEVEL_CHOICES).get(obj.level)

    def get_question_type_display(self, obj):
        return dict(Quiz.QUESTION_TYPE_CHOICES).get(obj.question_type)

    def get_answer_type_display(self, obj):
        return dict(Quiz.ANSWER_TYPE_CHOICES).get(obj.answer_type)
