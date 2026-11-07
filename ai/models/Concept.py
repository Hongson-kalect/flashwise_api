from django.db import models

from config.models import BaseModel


class Concept(BaseModel):
    """
    Tầng 1: một "ý" dùng chung giữa các nghĩa GIỐNG HỆT nhau ở mọi ngôn ngữ (big = lớn = grande). Danh tính nằm ở mô tả tiếng Anh + vector,
    nhãn `label` chỉ để đọc. Thiết kế và lý do: flashwise/docs/concept-design.md. Tầng 2 (đồ thị giữa các concept gần nghĩa) dựng sau từ ConceptDecision.near.
    """
    ANCHORED = 'anchored'          # có nghĩa tiếng Anh thật làm neo
    PROVISIONAL = 'provisional'    # tạo từ nghĩa nước ngoài, chưa có nghĩa tiếng Anh khớp
    LEXICAL_GAP = 'lexical_gap'    # không có từ tiếng Anh tương đương (sobremesa, 木漏れ日)
    STATUS_CHOICES = [(ANCHORED, 'Anchored'), (PROVISIONAL, 'Provisional'), (LEXICAL_GAP, 'Lexical gap')]

    label = models.CharField(max_length=255)
    description_en = models.TextField(blank=True, default='')
    embedding = models.BinaryField(null=True, blank=True)  # float32 đã chuẩn hoá, trung bình các thành viên
    member_count = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PROVISIONAL, db_index=True)
    pos_class = models.CharField(max_length=20, blank=True, default='')
    merged_into = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='merged_from')

    def __str__(self):
        return f'{self.label} [{self.status}]'


class ConceptDecision(BaseModel):
    """Nhật ký mỗi lần nối nghĩa vào concept: để kiểm toán, đo độ chính xác và làm đầu vào cho tầng 2 (near = gần nhưng khác)."""
    sense = models.ForeignKey('ai.AISense', on_delete=models.CASCADE, related_name='concept_decisions')
    concept = models.ForeignKey(Concept, on_delete=models.SET_NULL, null=True, blank=True, related_name='decisions')
    outcome = models.CharField(max_length=30, db_index=True)  # linked | created | created_gap | ambiguous_split | skipped
    cos = models.FloatField(null=True, blank=True)
    candidates = models.JSONField(default=list, blank=True)  # [{concept_id, label, cos}]
    runs = models.JSONField(default=list, blank=True)        # kết quả từng lần judge
    near = models.JSONField(default=list, blank=True)        # concept id gần nhưng khác (đầu vào tầng 2)
    reason = models.TextField(blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
