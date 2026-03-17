# urls.py trong app hoặc project
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.views import CollectionViewSet, LanguageViewSet, WordViewSet

router = DefaultRouter()
router.register(r'collections', CollectionViewSet, basename='collection')
router.register(r'words', WordViewSet, basename='word')
router.register(r'languages', LanguageViewSet, basename='language')

urlpatterns = router.urls