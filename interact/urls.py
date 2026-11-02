from django.urls import path

from interact.views import (
    create_feedback, create_report, moderation_ban, moderation_history, moderation_unban, report_queue, resolve_reports,
)

urlpatterns = [
    path('reports/', create_report),
    path('reports/queue/', report_queue),
    path('reports/resolve/', resolve_reports),
    path('moderation/', moderation_history),
    path('moderation/ban/', moderation_ban),
    path('moderation/unban/', moderation_unban),
    path('feedback/', create_feedback),
]
