# urls.py trong app hoặc project
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from ai.views.AISense import AISenseViewSet
from ai.views.AIWord import AIWordViewSet

router = DefaultRouter()
router.register(r'ai-words', AIWordViewSet, basename='ai-words')
router.register(r'ai-senses', AISenseViewSet, basename='ai-senses')

urlpatterns = router.urls