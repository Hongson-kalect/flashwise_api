# core/admin.py
from django.contrib import admin
from .models import Device,Notification,UserProfile,PurchaseRecord,RestrictList

admin.site.register(Device)
admin.site.register(Notification)
admin.site.register(UserProfile)
admin.site.register(PurchaseRecord)
admin.site.register(RestrictList)
