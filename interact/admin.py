# core/admin.py
from django.contrib import admin
from .models import Feedback,LearnSession,UserInteraction,Report,UpdateLog

admin.site.register(Feedback)
admin.site.register(LearnSession)
admin.site.register(UserInteraction)
admin.site.register(Report)
admin.site.register(UpdateLog)