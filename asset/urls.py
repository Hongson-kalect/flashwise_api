from django.urls import path

from asset.views import media, media_detail

urlpatterns = [
    path('media/', media),
    path('media/<str:pk>/', media_detail),
]
