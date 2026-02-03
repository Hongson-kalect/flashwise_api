from django.db import models

class Dashboard(models.Model):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    updated_at = models.DateTimeField(auto_now=True)
    summary = models.JSONField()
    language = models.ForeignKey('core.Language', on_delete=models.CASCADE)
    """
    summary: lưu dữ liệu tổng hợp như:
    {
      "xp_total": 12345,
      "words_learned": 678,
      "streak_days": 15,
      "chart_data": [...],
      ...
    }
    -> Được cập nhật định kỳ từ DailyLog, WeekLog, MonthLog.
    """
    class Meta:
        db_table = "dashboard_cache"