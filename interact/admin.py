# core/admin.py
from django.contrib import admin
from .models import Feedback,ForgetWord,LearnSession,LearnWord,LikeCollection,LikeWord,Report,UpdateLog,UpdateRequest,ViewCollection,ViewWord,WordStatus

admin.site.register(Feedback)
admin.site.register(ForgetWord)
admin.site.register(LearnSession)
admin.site.register(LearnWord)
admin.site.register(LikeCollection)
admin.site.register(LikeWord)
admin.site.register(Report)
admin.site.register(UpdateLog)
admin.site.register(UpdateRequest)
admin.site.register(ViewCollection)
admin.site.register(ViewWord)
admin.site.register(WordStatus)