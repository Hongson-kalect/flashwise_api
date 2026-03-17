from rest_framework import serializers
from django.contrib.auth import get_user_model
from cache.models.Dashboard import Dashboard  # hoặc đường dẫn đúng với project của bạn
from core.models import Language  # nếu bạn có model Language

User = get_user_model()

class DashboardSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    language = serializers.PrimaryKeyRelatedField(queryset=Language.objects.all())

    class Meta:
        model = Dashboard
        fields = [
            'user',
            'updated_at',
            'summary',
            'language',
        ]
        read_only_fields = ['updated_at']
