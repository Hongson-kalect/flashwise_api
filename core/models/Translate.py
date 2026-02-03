from django.db import models

from config.models import BaseModel

class Translate(BaseModel):
    sub_id = models.CharField(max_length=100, blank=True, null=True)
    value = models.CharField(max_length=255)
    word = models.ForeignKey('core.Word', on_delete=models.CASCADE, related_name='word_translates', null=True, blank=True)
    language_code = models.CharField(max_length=50, blank=True, null=True)
    is_auto = models.BooleanField(default=False)
    detail = models.ForeignKey('core.Word', on_delete=models.CASCADE, related_name='translate_details', null=True, blank=True)
    is_active = models.BooleanField(default=True)
    score = models.IntegerField(default=0, null=True)
    request_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='requested_translations')

    class Meta:
        db_table = 'translate'
        ordering = ['language_code', 'value']
        indexes=[
            models.Index(fields=['is_active','is_deleted','language_code','score']),
        ]

    def __str__(self):
        return f"Translate[{self.language_code}]"
    
    def save(self, *args, **kwargs):
        if not self.sub_id:
            self.sub_id = self.id
            # self.sub_id = str(uuid.uuid4())
        super().save(*args, **kwargs)
