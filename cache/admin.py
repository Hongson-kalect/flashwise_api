# core/admin.py
from django.contrib import admin
from .models import Dashboard,InterestingWord,MostForgetWord,MostLikeCollection,MostLikeWord,RecommendCollection

admin.site.register(Dashboard)
admin.site.register(InterestingWord)
admin.site.register(MostForgetWord)
admin.site.register(MostLikeCollection)
admin.site.register(MostLikeWord)
admin.site.register(RecommendCollection)