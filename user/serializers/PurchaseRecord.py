from rest_framework import serializers
from django.contrib.auth import get_user_model
from config.serializers.BaseModel import BaseModelSerializer
from user.models.PurchaseRecord import PurchaseRecord  # hoặc đường dẫn đúng với project của bạn

User = get_user_model()

class PurchaseRecordSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    platform_display = serializers.CharField(source='get_platform_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta(BaseModelSerializer.Meta):
        model = PurchaseRecord
        fields = BaseModelSerializer.Meta.fields + [
            'user',
            'platform',
            'platform_display',
            'product_id',
            'order_id',
            'purchase_token',
            'status',
            'status_display',
            'purchased_at',
            'expired_at',
            'amount',
            'currency',
            'receipt',
            'discount',
        ]
        read_only_fields = BaseModelSerializer.Meta.read_only_fields + ['purchased_at']
