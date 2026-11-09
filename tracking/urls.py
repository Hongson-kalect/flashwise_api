from django.urls import path

from tracking.views import fsrs_parameters, learn_new, learn_new_complete, learn_review, learn_review_complete, sync_push, sync_pull

urlpatterns = [
    path('sync/push/', sync_push),
    path('sync/pull/', sync_pull),
    path('learn/new/', learn_new),
    path('learn/review/', learn_review),
    path('learn/new/complete/', learn_new_complete),
    path('learn/review/complete/', learn_review_complete),
    path('learn/fsrs/', fsrs_parameters),
]
