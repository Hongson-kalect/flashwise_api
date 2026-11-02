"""
Nhận file user tải lên: kiểm tra loại / dung lượng, ảnh thì mở bằng Pillow (bảo đảm là ảnh thật), thu nhỏ và đổi sang
WebP (bỏ EXIF, vị trí GPS...), audio thì kiểm tra chữ ký đầu file. Lưu qua core/storages.py.
"""
import io

from django.conf import settings
from PIL import Image, ImageOps

from asset.models import MediaFile
from core import storages
from utils.utils.uuidv7 import generate_uuid7

# Chữ ký đầu file của các định dạng audio app ghi / chọn được (expo-av ghi m4a, Android có thể ra webm / ogg)
AUDIO_FORMATS = {
    'mp3': ('audio/mpeg', lambda b: b[:3] == b'ID3' or (b[0] == 0xFF and b[1] & 0xE0 == 0xE0)),
    'm4a': ('audio/mp4', lambda b: b[4:8] == b'ftyp'),
    'aac': ('audio/aac', lambda b: b[0] == 0xFF and b[1] & 0xF6 == 0xF0),
    'ogg': ('audio/ogg', lambda b: b[:4] == b'OggS'),
    'wav': ('audio/wav', lambda b: b[:4] == b'RIFF' and b[8:12] == b'WAVE'),
    'webm': ('audio/webm', lambda b: b[:4] == b'\x1a\x45\xdf\xa3'),
    'caf': ('audio/x-caf', lambda b: b[:4] == b'caff'),
}


class MediaError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def _read(upload, limit):
    if upload.size > limit:
        raise MediaError('file_too_large', f'Max {limit} bytes')
    data = upload.read()
    if len(data) > limit:
        raise MediaError('file_too_large', f'Max {limit} bytes')
    return data


def process_image(data):
    """Trả (bytes WebP, rộng, cao). Không phải ảnh / ảnh hỏng: MediaError."""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        raise MediaError('invalid_file', 'Not a valid image')
    if image.width * image.height > 40_000_000:
        raise MediaError('invalid_file', 'Image too large')
    image = ImageOps.exif_transpose(image)  # xoay đúng chiều trước khi bỏ EXIF
    image = image.convert('RGBA' if image.mode in ('RGBA', 'LA', 'P') else 'RGB')
    image.thumbnail((settings.MEDIA_IMAGE_MAX_SIDE, settings.MEDIA_IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format='WEBP', quality=80, method=4)
    return out.getvalue(), image.width, image.height


def audio_format(name, data):
    """(đuôi, content_type) theo chữ ký đầu file, đuôi tên file chỉ để chọn khi nhiều định dạng khớp."""
    if len(data) < 12:
        return None
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    matches = [(fmt, mime) for fmt, (mime, check) in AUDIO_FORMATS.items() if check(data)]
    for fmt, mime in matches:
        if fmt == ext:
            return fmt, mime
    return matches[0] if matches else None


def save_upload(user, kind, upload):
    """Kiểm tra + lưu 1 file, trả MediaFile. Lỗi: MediaError (code: invalid_kind / file_too_large / invalid_file)."""
    file_id = generate_uuid7()
    if kind == 'image':
        data, width, height = process_image(_read(upload, settings.MEDIA_IMAGE_MAX_BYTES))
        ext, content_type = 'webp', 'image/webp'
    elif kind == 'audio':
        data, width, height = _read(upload, settings.MEDIA_AUDIO_MAX_BYTES), None, None
        found = audio_format(upload.name or '', data)
        if found is None:
            raise MediaError('invalid_file', f'Audio must be one of {sorted(AUDIO_FORMATS)}')
        ext, content_type = found
    else:
        raise MediaError('invalid_kind', 'kind must be image or audio')

    key = f'media/{user.pk}/{file_id}.{ext}'
    storages.put(key, data, content_type)
    return MediaFile.objects.create(
        id=file_id, kind=kind, key=key, content_type=content_type, size=len(data), width=width, height=height, created_by=user,
    )


def owned_media_key(user, key, kind):
    """Khoá file user gửi kèm khi sửa nghĩa / bộ: phải là file của chính user (admin: file bất kỳ), đúng loại."""
    if not isinstance(key, str) or not key:
        return False
    rows = MediaFile.objects.filter(key=key, kind=kind).exclude(is_deleted=True)
    if not user.is_staff:
        rows = rows.filter(created_by=user)
    return rows.exists()


def clean_media_param(user, value, kind):
    """
    Giá trị ảnh / audio client gửi kèm: None = không đổi, '' = bỏ, khoá file của chính user (tải qua POST media/),
    ảnh thì cho thêm URL http(s) ngoài (ảnh Pixabay có sẵn). Trả (giá trị, lỗi hoặc None).
    """
    if value is None or value == '':
        return value, None
    if not isinstance(value, str) or len(value) > 1000:
        return None, f'Invalid {kind}'
    if kind == 'image' and value.startswith(('http://', 'https://')):
        return value, None
    if owned_media_key(user, value, kind):
        return value, None
    return None, f'Invalid {kind}: upload it via POST /api/media/ first'


def delete_media(media):
    try:
        storages.delete(media.key)
    finally:
        media.delete()
