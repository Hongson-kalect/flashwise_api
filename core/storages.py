import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def image_storage():
    # Callable để migration không ghi cứng đường dẫn tuyệt đối của máy đang chạy makemigrations
    return FileSystemStorage(location=os.path.join(settings.MEDIA_ROOT, 'images'))
