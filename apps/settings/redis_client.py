import os
import json
import redis
from django.conf import settings

# Get Redis connection from settings or fallback to env or default localhost
redis_url = getattr(settings, 'CELERY_BROKER_URL', None) or os.getenv('CELERY_BROKER_URL') or 'redis://localhost:6379/0'

# Initialize connection pool / client
redis_client = redis.Redis.from_url(redis_url, decode_responses=True)

REDIS_PREFIX = "caching_dev_setting:"


def sync_setting_to_redis(setting):
    """
    Saves or updates setting in Redis as JSON.
    """
    key = f"{REDIS_PREFIX}{setting.key}"
    data = {
        "value": setting.value,
        "enabled": setting.enabled
    }
    redis_client.set(key, json.dumps(data))


def delete_setting_from_redis(key):
    """
    Deletes setting from Redis.
    """
    redis_client.delete(f"{REDIS_PREFIX}{key}")


def get_setting_value(key):
    """
    Reads the setting value only from Redis.
    Returns None if the setting is disabled, deleted, or doesn't exist.
    No database fallback during runtime.
    """
    redis_key = f"{REDIS_PREFIX}{key}"
    try:
        data = redis_client.get(redis_key)
        if data:
            setting_info = json.loads(data)
            if setting_info.get("enabled", True):
                return setting_info.get("value")
    except Exception:
        # Fail silently to avoid blocking the application if Redis is down,
        # but do not fall back to DB.
        pass
    return None
