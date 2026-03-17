from django.apps import AppConfig


class TrackingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai'

    # def ready(self):
    #     import user.signals  # 👈 bắt buộc phải import ở đây!
