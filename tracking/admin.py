# core/admin.py
from django.contrib import admin
from .models import AdminLog,DayLearnLog,LearnLog

admin.site.register(AdminLog)


admin.site.register(DayLearnLog)
admin.site.register(LearnLog)