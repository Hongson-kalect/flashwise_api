from config.models import BaseModel
from django.db import models

class PurchaseRecord(BaseModel):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    platform = models.CharField(max_length=20, choices=[("google", "Google Play"), ("apple", "App Store")])
    product_id = models.CharField(max_length=100)
    order_id = models.CharField(max_length=200, unique=True)
    purchase_token = models.CharField(max_length=500, null=True, blank=True)
    status = models.CharField(max_length=20, choices=[("pending", "Pending"), ("success", "Success"), ("refunded", "Refunded")])
    purchased_at = models.DateTimeField()
    expired_at = models.DateTimeField(null=True, blank=True)
    
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10)
    receipt = models.CharField(max_length=500, null=True, blank=True)
    discount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)