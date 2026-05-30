from operator import index
from django.db import models
from ai.models.AISense import AISense

from config.models import BaseModel

class Quiz(BaseModel):
    LEVEL_CHOICES = [
        ('beginner', 'Beginner'),
        ('intermediate', 'Intermediate'),
        ('advanced', 'Advanced'),
    ]

    QUESTION_TYPE_CHOICES = [
        ('meaning', 'Meaning'),
        ('usage', 'Usage'),
    ]

    ANSWER_TYPE_CHOICES = [
        ('multiple-choice', 'Multiple Choice'),
        ('fill-in', 'Fill in'),
    ]

    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='beginner', db_index=True)
    type = models.CharField(max_length=100, blank=True, null=True) # Loại câu hỏi: Kiểu như là tìm từ khác âm, đồng nghĩa, hay mịa gì đấy, thường áp dụng để làm đề.
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES, default='meaning')
    answer_type = models.CharField(max_length=20, choices=ANSWER_TYPE_CHOICES, default='multiple-choice')
    
    question = models.TextField()
    options = models.JSONField(default=list, blank=True) # Danh sách đáp án gây nhiễu
    answer = models.JSONField(default=list, blank=True)  # Mảng đáp án đúng (Hỗ trợ multi-choice hoặc điền từ đồng nghĩa)
    explanation = models.TextField(blank=True)
    score = models.IntegerField(default=0, null=True)

    # ĐỔI TỪ WORD SANG SENSE: Trái tim liên kết đúng ngữ cảnh ngôn ngữ
    sense = models.ForeignKey(AISense, on_delete=models.CASCADE, related_name='quizzes')

    class Meta:
        db_table = 'quiz'
        ordering = ['created_at']
        indexes = [
            # INDEX CHÍ MẠNG TỐI ƯU API: Bốc nhanh toàn bộ Quiz thuộc về 1 Sense và lọc theo level học tập của user
            models.Index(fields=['sense', 'level', 'is_active', 'is_deleted'], name='idx_quiz_lookup'),
        ]

    def __str__(self):
        word_value = self.sense.word_value if self.sense else "Unknown"
        return f"Quiz ({self.question_type} - {self.level}) for Sense: {word_value}"