from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from apps.settings.models import CachingDevSetting
from apps.settings.redis_client import sync_setting_to_redis, delete_setting_from_redis


@receiver(post_save, sender=CachingDevSetting)
def handle_setting_save(sender, instance, **kwargs):
    """
    Sync setting to Redis when created or updated in DB.
    """
    sync_setting_to_redis(instance)


@receiver(post_delete, sender=CachingDevSetting)
def handle_setting_delete(sender, instance, **kwargs):
    """
    Delete setting from Redis when deleted in DB.
    """
    delete_setting_from_redis(instance.key)
