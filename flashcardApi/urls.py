"""
URL configuration for flashcardApi project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from utils.utils.get_token import add_bonus_word, add_kanji, add_word, dev_token, fix_data, jmdict, read_word

urlpatterns = [
    path('admin/', admin.site.urls),
    path('get-token/', dev_token),
    path('add-word/', add_word),
    path('add-bonus-word/', add_bonus_word),
    path('read-word/', read_word),
    path('fix-data/', fix_data),
    path('add-kanji/', add_kanji),
    # path('test-ai/', test),
    path('jmdict/', jmdict),
    
    # path('api/', include(('core.urls', 'flashcards.urls', 'ai.urls'))),
    path('api/', include('core.urls')),
    path('api/', include('ai.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
