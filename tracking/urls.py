from django.urls import path

from tracking.views import learn_new, learn_review, sync_push, sync_pull

urlpatterns = [
    path('sync/push/', sync_push),
    path('sync/pull/', sync_pull),
    path('learn/new/', learn_new),
    path('learn/review/', learn_review),
]
