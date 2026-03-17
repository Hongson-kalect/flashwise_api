from rest_framework import serializers
from config.serializers.BaseModel import BaseModelSerializer
from interact.models.Feedback import Feedback
from user.models import Notification
from django.contrib.auth import get_user_model

User = get_user_model()

class FeedbackSerializer(BaseModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    notification = serializers.PrimaryKeyRelatedField(queryset=Notification.objects.all(), allow_null=True, required=False)
    type_display = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()

    class Meta:
        model = Feedback
        fields = [
            'id', 'sub_id',
            'user', 'type', 'type_display',
            'result', 'accepted', 'notification',
            'message', 'target_id', 'status', 'status_display',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'sub_id', 'created_at', 'updated_at']

    def get_type_display(self, obj):
        return dict(Feedback.TYPE_CHOICES).get(obj.type)

    def get_status_display(self, obj):
        return dict(Feedback.STATUS_CHOICES).get(obj.status)
