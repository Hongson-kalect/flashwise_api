# core/admin.py
from django.contrib import admin
from .models import Feedback,LearnSession,UserInteraction,Report,UpdateLog

admin.site.register(Feedback)
admin.site.register(LearnSession)
admin.site.register(UserInteraction)


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    """Duyệt báo cáo: lọc theo trạng thái / loại, sửa trạng thái + phản hồi ngay ở danh sách. Nghĩa sai thì tạo lại từ qua ai-words/{id}/regenerate."""
    list_display = ('created_at', 'target_type', 'target_id', 'type', 'reason', 'status', 'result')
    list_filter = ('status', 'target_type', 'type')
    list_editable = ('status', 'result')
    search_fields = ('reason', 'message', 'target_id')
    date_hierarchy = 'created_at'
admin.site.register(UpdateLog)