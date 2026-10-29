from django.urls import path

from interact.views import create_feedback, create_report

urlpatterns = [
    path('reports/', create_report),
    path('feedback/', create_feedback),
]
