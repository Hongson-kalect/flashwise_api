# core/admin.py
from django.contrib import admin

from .models import Collection, CollectionItem, UserCollection, Language, Quiz
from .models.ImageLibrary import ImageContext

admin.site.register(Collection)
admin.site.register(CollectionItem)
admin.site.register(UserCollection)
admin.site.register(Language)
admin.site.register(Quiz)


class _HasListFilter(admin.SimpleListFilter):
    field = ''

    def lookups(self, request, model_admin):
        return [('yes', 'có'), ('no', 'chưa')]

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(**{self.field: []})
        if self.value() == 'no':
            return queryset.filter(**{self.field: []})
        return queryset


class HumanVerifiedFilter(_HasListFilter):
    title, parameter_name, field = 'người verify', 'human_verify', 'verify'


class AIVerifiedFilter(_HasListFilter):
    title, parameter_name, field = 'AI verify', 'ai_verify_has', 'ai_verify'


@admin.register(ImageContext)
class ImageContextAdmin(admin.ModelAdmin):
    list_display = ('description', 'provider', 'verified', 'ai_verified')
    list_filter = (HumanVerifiedFilter, AIVerifiedFilter, 'provider')
    search_fields = ('description',)

    @admin.display(description='verify')
    def verified(self, obj):
        return ', '.join(obj.verify or []) or '-'

    @admin.display(description='ai_verify')
    def ai_verified(self, obj):
        return ', '.join(obj.ai_verify or []) or '-'
