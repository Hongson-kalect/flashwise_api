from django.urls import path

from tracking.views import sync_push, sync_pull

urlpatterns = [
    path('sync/push/', sync_push),
    path('sync/pull/', sync_pull),
]
