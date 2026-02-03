# core/admin.py
from django.contrib import admin
from .models import EntityLink,EntityTag,SnapShot,Tag,Topic,Version

admin.site.register(EntityLink)
admin.site.register(EntityTag)
admin.site.register(SnapShot)
admin.site.register(Tag)
admin.site.register(Topic)
admin.site.register(Version)