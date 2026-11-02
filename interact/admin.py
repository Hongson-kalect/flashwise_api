# core/admin.py
from django.contrib import admin
from .models import Feedback,LearnSession,UserInteraction,Report,UpdateLog,ModerationAction

admin.site.register(Feedback)
admin.site.register(LearnSession)
admin.site.register(UserInteraction)


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    """Duyệt báo cáo: lọc theo trạng thái / loại, sửa trạng thái + phản hồi ngay ở danh sách. Nghĩa sai thì tạo lại từ qua ai-words/{id}/regenerate."""
    list_display = ('created_at', 'target_type', 'target_id', 'type', 'reason', 'weight', 'status', 'outcome', 'result')
    list_filter = ('status', 'outcome', 'target_type', 'type')
    list_editable = ('status', 'result')
    search_fields = ('reason', 'message', 'target_id')
    date_hierarchy = 'created_at'
admin.site.register(UpdateLog)

@admin.register(ModerationAction)
class ModerationActionAdmin(admin.ModelAdmin):
    """Lịch sử ban / gỡ ban; ban qua API moderation/ban để trạng thái và uy tín được cập nhật đúng."""
    list_display = ('created_at', 'action', 'target_type', 'target_id', 'reason', 'expires_at', 'created_by')
    list_filter = ('action', 'target_type')
    search_fields = ('target_id', 'reason')
