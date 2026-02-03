from django.apps import AppConfig


class CacheConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'asset'

    # def ready(self):
    #     import user.signals  # 👈 bắt buộc phải import ở đây!
