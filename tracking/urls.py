from django.urls import path

from tracking.views import learn_session, sync_push, sync_pull

urlpatterns = [
    path('sync/push/', sync_push),
    path('sync/pull/', sync_pull),
    path('learn/session/', learn_session),
]
