import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def image_storage():
    # Callable để migration không ghi cứng đường dẫn tuyệt đối của máy đang chạy makemigrations
    # base_url phải khớp thư mục con 'images', nếu không url sinh ra (/upload/v1/...) trỏ sai chỗ file nằm (upload/images/v1/...)
    return FileSystemStorage(location=os.path.join(settings.MEDIA_ROOT, 'images'), base_url=settings.MEDIA_URL + 'images/')
