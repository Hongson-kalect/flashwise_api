# models.py

from django.db import models

from config.models import BaseModel

class Ruby(BaseModel):
    """
    Model lưu trữ thông tin chi tiết về từng ký tự Kanji
    (Tên bảng được đặt là 'Ruby' theo yêu cầu của bạn).
    """

    # --- 1. Dữ liệu Cơ bản và Phát âm ---
    
    # Ký tự Kanji (Khóa tra cứu chính)
    value = models.CharField(
        max_length=10,
        unique=True,
        verbose_name="Ký tự Kanji",
        help_text="Ký tự Hán tự đơn lẻ (Kanji, Hanzi, Hanja)."
    )
    
    # Cách đọc On'yomi (Âm Hán - Katakana)
    on_yomi = models.TextField(
        blank=True,
        verbose_name="On'yomi",
        help_text="Âm Hán (cách đọc có nguồn gốc từ tiếng Trung)."
    )
    
    # Cách đọc Kun'yomi (Âm Nhật - Hiragana)
    kun_yomi = models.TextField(
        blank=True,
        verbose_name="Kun'yomi",
        help_text="Âm Nhật (cách đọc thuần Nhật)."
    )
    
    # Phân loại chính thức (jouyou, jinmeiyou, v.v.)
    type = models.CharField(
        max_length=20,
        blank=True,
        verbose_name="Phân loại"
    )

    # Các nghĩa tiếng Anh (Lưu dưới dạng mảng/JSONB) [{language: "en", value: "..."}, ...]
    meanings = models.JSONField(
        default=list,
        verbose_name="Nghĩa tiếng Anh",
        help_text="Danh sách các nghĩa tiếng Anh."
    )

    # grade_leveljlpt_levelstroke_countfrequency_rank
    metadata = models.JSONField(
        default=dict,
        blank=True,null=True)

    class Meta:
        verbose_name = "Kanji Ruby"
        verbose_name_plural = "Kanji Ruby Characters"
        # Đặt tên bảng cơ sở dữ liệu (tùy chọn)
        db_table = 'ruby' 

    def __str__(self):
        # Trả về ký tự Kanji làm đại diện cho đối tượng
        return self.value