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
KINDS = ('object', 'action', 'scene')  # 'abstract' (dữ liệu cũ) không vẽ: ý niệm không có hình thì vẽ ra là sai nghĩa

# Phong cách KHÔNG cần LoRA (đo 4 biến thể trên 10 chủ thể: docs/image-tests/25 của app): icon 2D kiểu tài sản game, viền navy dày,
# tô hai tông (một tông bóng, một tông sáng) + điểm sáng bóng nên vật có màu vật liệu đúng (đá nâu, xô / thìa kim loại xám), không còn
# mảng màu xanh loang. Phần tối dùng navy thay đen, da màu đào tự nhiên. Nền là màu đơn sắc KEY (magenta, xem cutout.py) để xoá nền
# sạch rồi đặt lên nền sáng của app; chủ thể không được dùng hồng / tím để khỏi lẫn với nền.
# Đổi chuỗi này thì tăng IMAGE_GEN_STYLE_VERSION.
STYLE = ("polished 2D game-asset icon illustration, thick clean dark navy outline, rich saturated colors, glossy highlights, "
         "two-tone cel shading with a darker shadow tone and a lighter highlight tone, subtle surface detail and texture, "
         "rounded friendly proportions, natural light peach skin tones, deep navy instead of black, "
         "solid flat pure magenta #FF00FF background, no pink or purple on the subject, one whole subject floating centered in empty space "
         "with generous margin and nothing underneath it, no text, no letters, no watermark, no glow, no drop shadow, no ground shadow")

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


def build_prompt(subject, description=None):
    """Prompt vẽ: mô tả riêng (image_prompt do Gemini viết) nếu có, không thì chủ thể ngắn; rồi STYLE."""
    text = (description or '').strip() or subject.strip()
    return f"{text.rstrip('.')}. {STYLE}"


def seed_for(key):
    """Seed cố định theo khoá: vẽ lại cùng chủ thể ra cùng ảnh (dễ so sánh khi đổi cấu hình)."""
    return int(hashlib.sha1(key.encode('utf-8')).hexdigest()[:8], 16)


def provider_id(key):
    """Mã ảnh trong ImageLibrary (unique cùng provider): theo khoá + phiên bản phong cách."""
    return hashlib.sha1(f'{settings.IMAGE_GEN_STYLE_VERSION}:{key}'.encode('utf-8')).hexdigest()[:32]
