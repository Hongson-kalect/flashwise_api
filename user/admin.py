# core/admin.py
from django.contrib import admin
from .models import Asset,BanList,Device,Login,LoginProvider,Notification,Profile,PurchaseRecord,RestrictList,Setting,Stat,Token

admin.site.register(Asset)
admin.site.register(BanList)
admin.site.register(Device)
admin.site.register(Login)
admin.site.register(LoginProvider)
admin.site.register(Notification)
admin.site.register(Profile)
admin.site.register(PurchaseRecord)
admin.site.register(RestrictList)
admin.site.register(Setting)
admin.site.register(Stat)
admin.site.register(Token)