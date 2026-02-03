from operator import index
from django.db import models

from config.models import BaseModel

class Quiz(BaseModel):
    sub_id = models.CharField(max_length=100, blank=True, null=True)
    LEVEL_CHOICES = [
        ('beginner', 'Beginner'),
        ('intermediate', 'Intermediate'),
        ('advanced', 'Advanced'),
    ]

    QUESTION_TYPE_CHOICES = [
        ('meaning', 'Meaning'),
        ('usage', 'Usage'),
    ]

    level = models.CharField(max_length=50, choices=LEVEL_CHOICES, blank=True, null=True)
    question_type = models.CharField(max_length=50, choices=QUESTION_TYPE_CHOICES, blank=True, null=True)
    question = models.TextField()
    options = models.JSONField(default=list, blank=True)
    answer = models.JSONField(default=list, blank=True)  # hỗ trợ nhiều đáp án
    ANSWER_TYPE_CHOICES = [
        ('multiple-choice', 'Multiple Choice'),
        ('fill-in', 'Fill in'),
    ]

    answer_type = models.CharField(max_length=50, choices=ANSWER_TYPE_CHOICES, blank=True, null=True)
    explanation = models.TextField(blank=True)
    word = models.ForeignKey('core.Word', on_delete=models.CASCADE, related_name='word_quizzes')
    score = models.IntegerField(default=0, null=True)

    class Meta:
        db_table = 'quiz'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f"Quiz({self.question_type}) for {self.word.value}"

    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)