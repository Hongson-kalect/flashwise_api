# core/admin.py
from django.contrib import admin
from .models import Image,Theme

admin.site.register(Image)
admin.site.register(Theme)