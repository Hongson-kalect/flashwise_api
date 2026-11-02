# core/admin.py
from django.contrib import admin
from .models import Device,Notification,UserProfile,PurchaseRecord,RestrictList,ReputationEvent

admin.site.register(Device)
admin.site.register(Notification)
admin.site.register(UserProfile)
admin.site.register(PurchaseRecord)
admin.site.register(RestrictList)


@admin.register(ReputationEvent)
class ReputationEventAdmin(admin.ModelAdmin):
    # Chỉ xem; cộng / trừ tay dùng user.reputation.record(user, "manual", points=...) để điểm được tính lại
    list_display = ('created_at', 'user', 'kind', 'points', 'target_type', 'target_id', 'note')
    list_filter = ('kind',)
    search_fields = ('user__username', 'note')
