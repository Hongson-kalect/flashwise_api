"""
Chủ thể cần vẽ cho 1 nghĩa và khoá dùng lại ảnh.

Khoá ảnh (`image_key`) là chủ thể bằng TIẾNG ANH đã chuẩn hoá, không phụ thuộc ngôn ngữ của từ: nghĩa "cat" (en) và
"猫" (ja) cùng sinh chủ thể "cat" thì chung 1 ImageContext / 1 ảnh, từ ngôn ngữ thứ hai không tốn thêm lần vẽ nào.
Chủ thể do bước metadata (Gemini) viết kèm `image_kind`, prompt metadata dặn dùng cùng 1 cách viết cho cùng 1 ý
và phân biệt từ đồng âm ("bat animal" / "baseball bat") nên khoá khác nhau cho nghĩa khác nhau.
"""
import hashlib
import re
import unicodedata

from django.conf import settings

# Loại nghĩa vẽ được. 'none' (từ ngữ pháp, tên riêng, thứ không vẽ được) hoặc thiếu = không vẽ
KINDS = ('object', 'action', 'scene', 'abstract')

STYLE = ("flat vector illustration, children's educational flashcard style, clean smooth shapes, "
         "medium-saturated friendly colors, medium-weight dark gray outline, soft simple shading, "
         "plain pure white background, one centered subject, no text, no letters, no watermark, no glow, no shadow")

_ARTICLES = re.compile(r'^(?:a|an|the)\s+')
_JUNK = re.compile(r'[^\w\s-]', re.UNICODE)


def image_key(subject):
    """Chủ thể chuẩn hoá: chữ thường, bỏ dấu câu và mạo từ đầu câu, gộp khoảng trắng. '' nếu rỗng."""
    if not isinstance(subject, str):
        return ''
    text = unicodedata.normalize('NFKC', subject).lower()
    text = _JUNK.sub(' ', text)
    text = ' '.join(text.split())
    text = _ARTICLES.sub('', text)
    return text[:120].strip()


def build_prompt(subject):
    return f"{subject.strip().rstrip('.')}. {STYLE}"


def seed_for(key):
    """Seed cố định theo khoá: vẽ lại cùng chủ thể ra cùng ảnh (dễ so sánh khi đổi cấu hình)."""
    return int(hashlib.sha1(key.encode('utf-8')).hexdigest()[:8], 16)


def provider_id(key):
    """Mã ảnh trong ImageLibrary (unique cùng provider): theo khoá + phiên bản phong cách."""
    return hashlib.sha1(f'{settings.IMAGE_GEN_STYLE_VERSION}:{key}'.encode('utf-8')).hexdigest()[:32]
