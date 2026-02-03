
from django.db import models

from config.models import BaseModel

class Level(BaseModel):
    """
    Bảng mức độ học hoặc độ khó (L1, L2, ... hoặc custom).
    order: thứ tự sắp xếp hiển thị.
    is_default: true nếu là level mặc định.
    """
    name = models.CharField(max_length=50, unique=True)
    nap_time = models.IntegerField(default=0, help_text="Thời gian nghỉ hoặc chờ giữa các session (giây hoặc phút).")
    description = models.TextField(blank=True, null=True)
    order = models.PositiveIntegerField(default=0)
    is_default = models.BooleanField(default=False)

    class Meta:
        db_table = "level"
        ordering = ["order", "name"]
        indexes = [
            models.Index(fields=["is_default"]),
            models.Index(fields=["order"]),
        ]

    def __str__(self):
        return f"Level({self.name})"