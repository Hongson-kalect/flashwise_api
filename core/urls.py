# urls.py trong app hoặc project
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.views import CollectionViewSet, LanguageViewSet, UserCollectionViewSet
from core.views.discover import my_uploads, suggested_collections, tags

router = DefaultRouter()
router.register(r'user-collections', UserCollectionViewSet, basename='user-collection')
router.register(r'collections', CollectionViewSet, basename='collection')
router.register(r'languages', LanguageViewSet, basename='language')

urlpatterns = [
    path('tags/', tags),
    path('collections/suggested/', suggested_collections),  # trước router: không thì bị hiểu là collections/{pk}/
    path('me/uploads/', my_uploads),
] + router.urls