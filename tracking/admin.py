# tracking/admin.py
from django.contrib import admin
from .models import AdminLog, DayLearnLog, ReviewLog, UserSenseProgress

admin.site.register(AdminLog)


admin.site.register(DayLearnLog)
admin.site.register(ReviewLog)
admin.site.register(UserSenseProgress)
