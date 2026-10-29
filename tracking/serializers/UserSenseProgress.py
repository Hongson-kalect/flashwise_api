from rest_framework import serializers

from tracking.models.UserSenseProgress import UserSenseProgress

class UserSenseProgressSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = UserSenseProgress
        fields = [
            'id', 'user', 'original_sense_id',
            'level', 'max_level',
            'streak_correct', 'forget_count', 'remember_count', 'repetitions',
            'state', 'stability', 'difficulty', 'failed_at',
            'is_mastered', 'is_hidden', 'is_avoid', 'is_deleted',
            'last_seen_at', 'last_reviewed_at', 'next_review_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['user', 'created_at', 'updated_at']
