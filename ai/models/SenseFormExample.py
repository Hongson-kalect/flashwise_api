from django.db import models

from config.models import BaseModel


class SenseFormExample(BaseModel):
    """
    Ví dụ 1 câu riêng của 1 dạng biến cách (buying) dưới nghĩa của từ gốc (buy). Không phải nghĩa mới: sửa / tách nghĩa gốc thì
    ví dụ vẫn gắn đúng nghĩa. Mỗi (nghĩa, dạng) tạo một lần, dùng chung cho mọi người dùng; chỉ lưu câu đã kiểm chứng
    bằng code là có chứa đúng dạng đó (ai/form_examples.py).
    """
    sense = models.ForeignKey('ai.AISense', on_delete=models.CASCADE, related_name='form_examples')
    form = models.CharField(max_length=255)
    language_code = models.CharField(max_length=10)
    text = models.TextField()
    translations = models.JSONField(default=dict, blank=True)  # {mã ngôn ngữ: bản dịch}

    class Meta:
        db_table = "sense_form_example"
        constraints = [
            models.UniqueConstraint(fields=["sense", "form"], name="unique_sense_form_example"),
        ]

    def __str__(self):
        return f"{self.form}: {self.text[:50]}"
