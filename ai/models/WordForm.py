from django.db import models

from config.models import BaseModel


class WordForm(BaseModel):
    """
    Dạng biến cách của 1 từ gốc theo loại từ: 'ran' = verb.past của run. Một chuỗi có thể có nhiều dòng
    (khác từ gốc, loại từ hoặc nhãn: runs = noun.plural của run / verb.3sg của run). Chuỗi trùng từ gốc không có dòng riêng.
    Chỉ ghi dữ liệu đã kiểm chứng: sinh từ thư viện biến cách (source=lib) hoặc nhận ra khi tra và từ gốc có trong DB (source=lookup).
    Nhãn chuẩn xem utils/morph/__init__.py.
    """
    SOURCES = [('lib', 'lib'), ('lookup', 'lookup')]

    word = models.ForeignKey('ai.AIWord', on_delete=models.CASCADE, related_name='forms')
    language_code = models.CharField(max_length=10)
    form = models.CharField(max_length=255)
    pos = models.CharField(max_length=20, blank=True, default='')  # noun | verb | adj | adv, rỗng nếu chưa rõ
    tag = models.CharField(max_length=60)
    irregular = models.BooleanField(default=False)
    source = models.CharField(max_length=10, choices=SOURCES, default='lib')

    class Meta:
        db_table = "word_form"
        ordering = ["pos", "tag", "form"]
        indexes = [
            models.Index(fields=["language_code", "form"], name="idx_wordform_lang_form"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["word", "pos", "tag", "form"], name="unique_wordform"),
        ]

    def __str__(self):
        return f"{self.form} = {self.tag} of {self.word_id}"
