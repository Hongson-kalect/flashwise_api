from django.apps import AppConfig


class InteractConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'interact'

    # def ready(self):
    #     import user.signals  # 👈 bắt buộc phải import ở đây!
