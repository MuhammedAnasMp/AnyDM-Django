from django.db import models


class CachingDevSetting(models.Model):
    key = models.CharField(
        max_length=255,
        unique=True
    )

    value = models.TextField(
        null=True,
        blank=True
    )

    enabled = models.BooleanField(
        default=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        db_table = 'caching_dev_settings'
        verbose_name = 'Caching Dev Setting'
        verbose_name_plural = 'Caching Dev Settings'

    def __str__(self):
        return self.key


class SystemSettings(models.Model):
    trial_days = models.IntegerField(default=14)
    extend_days = models.IntegerField(default=7)
    referral_points = models.IntegerField(default=50)
    points_to_redeem = models.IntegerField(default=100)
    premium_plan_price = models.DecimalField(max_digits=10, decimal_places=2, default=499.00)

    # Global AI options
    enable_ai = models.BooleanField(default=True)
    enable_subscription_ai = models.BooleanField(default=False)
    business_gemini_api_key = models.TextField(blank=True, default="")

    # Marketplace configurations
    global_cod_enabled = models.BooleanField(default=True)
    default_commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=10.00)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'System Settings'
        verbose_name_plural = 'System Settings'

    @classmethod
    def get_settings(cls):
        settings, created = cls.objects.get_or_create(id=1)
        return settings

    def __str__(self):
        return f"SystemSettings (Price: {self.premium_plan_price}, Referral Points: {self.referral_points})"
