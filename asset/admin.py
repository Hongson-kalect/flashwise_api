# core/admin.py
from django.contrib import admin
from .models import Image, MediaFile, Theme

admin.site.register(Image)
admin.site.register(Theme)


@admin.register(MediaFile)
class MediaFileAdmin(admin.ModelAdmin):
    list_display = ('key', 'kind', 'size', 'created_by', 'created_at', 'is_active')
    list_filter = ('kind', 'is_active')
    search_fields = ('key',)