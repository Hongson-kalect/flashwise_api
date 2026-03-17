from django.apps import AppConfig


class ConfigConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'config'

    # def ready(self):
    #     import user.signals  # 👈 bắt buộc phải import ở đây!
