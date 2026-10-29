# core/admin.py
from django.contrib import admin

from .models import Collection, CollectionItem, UserCollection, Language, Quiz

admin.site.register(Collection)
admin.site.register(CollectionItem)
admin.site.register(UserCollection)
admin.site.register(Language)
admin.site.register(Quiz)
