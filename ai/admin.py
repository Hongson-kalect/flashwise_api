# core/admin.py
from django.contrib import admin
from .models import AIWord ,AISenseContent,AISenseMetadata ,AISense
from .models.Concept import Concept

admin.site.register(AIWord)
admin.site.register(AISenseContent)


class HasListFilter(admin.SimpleListFilter):
    """Lọc theo cột JSON danh sách (verify / ai_verify): có ít nhất một ngôn ngữ hay chưa có."""
    field = ''

    def lookups(self, request, model_admin):
        return [('yes', 'có'), ('no', 'chưa')]

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(**{self.field: []})
        if self.value() == 'no':
            return queryset.filter(**{self.field: []})
        return queryset


class HumanVerifiedFilter(HasListFilter):
    title, parameter_name, field = 'người verify', 'human_verify', 'verify'


class AIVerifiedFilter(HasListFilter):
    title, parameter_name, field = 'AI verify', 'ai_verify_has', 'ai_verify'


def _langs(value):
    return ', '.join(value or []) or '-'


@admin.register(AISense)
class AISenseAdmin(admin.ModelAdmin):
    # verify: các ngôn ngữ người đã kiểm (sửa tay, danh sách mã như ["vi","en"]); ai_verify: ngôn ngữ model thứ hai đồng ý (ghi bởi ai/seedflow/verify.py)
    list_display = ('word_value', 'pos', 'level', 'language_code', 'verified', 'ai_verified', 'quality_status')
    list_filter = (HumanVerifiedFilter, AIVerifiedFilter, 'quality_status', 'language_code', 'pos')
    search_fields = ('word_value',)
    readonly_fields = ('ai_verify', 'verify_state')

    @admin.display(description='verify')
    def verified(self, obj):
        return _langs(obj.verify)

    @admin.display(description='ai_verify')
    def ai_verified(self, obj):
        return _langs(obj.ai_verify)


@admin.register(AISenseMetadata)
class AISenseMetadataAdmin(admin.ModelAdmin):
    list_display = ('id', 'verified', 'ai_verified')
    list_filter = (HumanVerifiedFilter, AIVerifiedFilter)

    @admin.display(description='verify')
    def verified(self, obj):
        return _langs(obj.verify)

    @admin.display(description='ai_verify')
    def ai_verified(self, obj):
        return _langs(obj.ai_verify)


@admin.register(Concept)
class ConceptAdmin(admin.ModelAdmin):
    list_display = ('label', 'status', 'pos_class', 'member_count', 'verified', 'ai_verified')
    list_filter = (HumanVerifiedFilter, AIVerifiedFilter, 'status')
    search_fields = ('label',)

    @admin.display(description='verify')
    def verified(self, obj):
        return _langs(obj.verify)

    @admin.display(description='ai_verify')
    def ai_verified(self, obj):
        return _langs(obj.ai_verify)

# admin.site.register(DayLearnLog)
# admin.site.register(LearnLog)
# admin.site.register(ModifierLog)
# admin.site.register(MonthLearnLog)
# # admin.site.register(QueryLog)
# @admin.register(QueryLog)
# class QueryConfig(admin.ModelAdmin):
#     list_display = ('user', 'target_type', 'target_id', 'created_at', 'is_success')
#     list_filter = ('target_type', 'is_success')  # ✅ cần là tuple, có dấu phẩy
#     search_fields = ('path', 'meta')
#     ordering = ('-created_at',)

# admin.site.register(WeekLearnLog)
# admin.site.register(YearLearnLog)