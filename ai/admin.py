# core/admin.py
from django.contrib import admin
from .models import AIWord ,AISenseContent,AISenseMetadata ,AISense

admin.site.register(AIWord)
admin.site.register(AISenseContent)
admin.site.register(AISenseMetadata)
admin.site.register(AISense)

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