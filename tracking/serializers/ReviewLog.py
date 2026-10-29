from rest_framework import serializers

from tracking.models.ReviewLog import ReviewLog

class ReviewLogSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = ReviewLog
        fields = [
            'id', 'user', 'original_sense_id', 'sense_id', 'session_id',
            'rating', 'mode', 'duration_ms',
            'state', 'elapsed_days', 'scheduled_days', 'stability', 'difficulty',
            'reviewed_at', 'created_at',
        ]
        read_only_fields = ['user', 'created_at']
