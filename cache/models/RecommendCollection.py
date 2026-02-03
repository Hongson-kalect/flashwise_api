from django.db import models

from core.models import Collection, Language

class RecommendCollection(models.Model):
    """
    Lưu cache danh sách gợi ý cho người dùng.
    - strategy: 'system' => gợi ý chung; 'personalized' => dựa trên hành vi học của user.
    - recommended_word_ids: danh sách ID word được gợi ý.
    """
    # user = models.ForeignKey('User', on_delete=models.CASCADE)
    language = models.ForeignKey('core.Language', on_delete=models.CASCADE)
    collection = models.ForeignKey('core.Collection', on_delete=models.CASCADE)
    collection_sub_id = models.CharField(max_length=50)
    strategy = models.CharField(max_length=32, choices=[
        ('system', 'System'),
        ('personalized', 'Personalized'),
    ])
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recommendation_cache"
