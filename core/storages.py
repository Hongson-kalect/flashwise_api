"""
Nơi lưu file (ảnh, audio): Cloudflare R2 khi có đủ R2_* trong env, không thì thư mục MEDIA_ROOT trên đĩa.
Mọi file có 1 khoá tương đối (vd images/v1/pixabay_1.webp); DB lưu khoá này, URL đầy đủ = MEDIA_PUBLIC_URL + khoá
(app lấy media_base_url ở app-config). Đổi nơi lưu: chép file sang chỗ mới + đổi env, dữ liệu trong DB giữ nguyên.
"""
import mimetypes
import os

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

_r2_client = None


def _r2():
    global _r2_client
    if _r2_client is None:
        import boto3  # chỉ cần khi dùng R2
        _r2_client = boto3.client(
            's3', endpoint_url=f'https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
            aws_access_key_id=settings.R2_ACCESS_KEY_ID, aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY, region_name='auto',
        )
    return _r2_client


def _local_path(key):
    path = os.path.normpath(os.path.join(settings.MEDIA_ROOT, key))
    if not path.startswith(os.path.normpath(settings.MEDIA_ROOT)):
        raise ValueError('Invalid media key')
    return path


def put(key, data, content_type=None):
    content_type = content_type or mimetypes.guess_type(key)[0] or 'application/octet-stream'
    if settings.R2_ENABLED:
        _r2().put_object(Bucket=settings.R2_BUCKET, Key=key, Body=data, ContentType=content_type,
                         CacheControl='public, max-age=31536000, immutable')  # khoá không bao giờ ghi đè nội dung khác
        return key
    path = _local_path(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return key


def get(key):
    if settings.R2_ENABLED:
        return _r2().get_object(Bucket=settings.R2_BUCKET, Key=key)['Body'].read()
    with open(_local_path(key), 'rb') as f:
        return f.read()


def exists(key):
    if settings.R2_ENABLED:
        try:
            _r2().head_object(Bucket=settings.R2_BUCKET, Key=key)
            return True
        except Exception:
            return False
    return os.path.exists(_local_path(key))


def delete(key):
    if settings.R2_ENABLED:
        _r2().delete_object(Bucket=settings.R2_BUCKET, Key=key)
    elif os.path.exists(_local_path(key)):
        os.remove(_local_path(key))


def media_url(key):
    """URL đầy đủ của 1 khoá (giá trị đã là http(s) thì giữ nguyên)."""
    if not key or key.startswith(('http://', 'https://')):
        return key
    return settings.MEDIA_PUBLIC_URL + key.lstrip('/')


@deconstructible
class MediaStorage(Storage):
    """Storage của Django trên put / get / delete ở trên; url() trả khoá tương đối (không kèm domain)."""

    def __init__(self, prefix=''):
        self.prefix = prefix

    def _key(self, name):
        return self.prefix + name.replace('\\', '/')

    def _save(self, name, content):
        content.seek(0)
        put(self._key(name), content.read())
        return name

    def _open(self, name, mode='rb'):
        return ContentFile(get(self._key(name)), name=name)

    def exists(self, name):
        return exists(self._key(name))

    def delete(self, name):
        delete(self._key(name))

    def url(self, name):
        return self._key(name)


def image_storage():
    # Callable để migration không ghi cứng nơi lưu; ảnh Pixabay nằm dưới images/ (cùng chỗ thư mục upload/images cũ)
    return MediaStorage('images/')
