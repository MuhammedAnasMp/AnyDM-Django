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
