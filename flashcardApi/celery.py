
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'flashcardApi.settings')

app = Celery('flashcardApi')

# namespace='CELERY' nghĩa là tất cả cấu hình trong settings.py 
# phải bắt đầu bằng chữ CELERY_ (ví dụ: CELERY_BROKER_URL)
app.config_from_object('django.conf:settings', namespace='CELERY')

app.autodiscover_tasks()