"""
Xoá nền magenta cho ảnh vẽ bởi FLUX (chroma key): ảnh được vẽ trên nền magenta thuần, màu không có trong bảng màu của
STYLE, nên xoá được cả vùng nền bị bao kín (trong quai xô, giữa cánh tay) mà không đụng tới màu trắng thật bên trong đồ vật.

FLUX không vẽ đúng #FF00FF từng điểm (nền hơi đổi sắc, bóng đổ dưới chân là magenta tối) nên không so khoảng cách với một màu
cố định mà đo "độ magenta" của từng pixel: min(R, B) - G. Nền và bóng magenta cho giá trị lớn, màu của chủ thể (xanh dương,
xanh lá, cam, da, nâu, trắng, navy) cho giá trị nhỏ hoặc âm. Mép lai được trừ bớt màu magenta (despill) để không ám hồng.
"""
import io

import numpy as np
from PIL import Image

CLEAR = 70    # độ magenta từ mức này trở lên: nền (trong suốt hẳn)
SOLID = 25    # dưới mức này: chủ thể (đặc hẳn); ở giữa: mép lai


def to_transparent(png_bytes, clear=CLEAR, solid=SOLID):
    """Trả Image RGBA từ bytes ảnh vẽ trên nền magenta."""
    rgb = np.asarray(Image.open(io.BytesIO(png_bytes)).convert('RGB')).astype(np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    spill = np.minimum(r, b) - g                                   # độ magenta
    alpha = np.clip((clear - spill) / float(clear - solid), 0.0, 1.0)
    # despill: mép lai bị ám magenta thì kéo R và B xuống bằng phần dư so với G
    edge = (alpha < 1.0) & (alpha > 0.0)
    cut = np.where(edge, np.maximum(spill, 0.0), 0.0)
    rgb[..., 0] = np.clip(r - cut, 0, 255)
    rgb[..., 2] = np.clip(b - cut, 0, 255)
    out = np.dstack([rgb, alpha * 255.0]).astype(np.uint8)
    return Image.fromarray(out, 'RGBA')
