from django.test import TestCase
from unittest.mock import patch
from apps.settings.models import CachingDevSetting
from apps.settings.redis_client import get_setting_value


class CachingDevSettingsTest(TestCase):
    @patch('apps.settings.redis_client.redis_client')
    def test_sync_on_save(self, mock_redis):
        # Test that saving a CachingDevSetting triggers redis set
        setting = CachingDevSetting.objects.create(
            key="test_key",
            value="test_value",
            enabled=True
        )
        expected_key = "caching_dev_setting:test_key"
        mock_redis.set.assert_called_with(expected_key, '{"value": "test_value", "enabled": true}')

    @patch('apps.settings.redis_client.redis_client')
    def test_sync_on_update(self, mock_redis):
        setting = CachingDevSetting.objects.create(
            key="test_key",
            value="test_value",
            enabled=True
        )
        mock_redis.reset_mock()
        
        setting.value = "new_value"
        setting.save()
        
        expected_key = "caching_dev_setting:test_key"
        mock_redis.set.assert_called_with(expected_key, '{"value": "new_value", "enabled": true}')

    @patch('apps.settings.redis_client.redis_client')
    def test_delete_from_redis(self, mock_redis):
        setting = CachingDevSetting.objects.create(
            key="test_key",
            value="test_value",
            enabled=True
        )
        mock_redis.reset_mock()
        
        setting.delete()
        
        expected_key = "caching_dev_setting:test_key"
        mock_redis.delete.assert_called_with(expected_key)

    @patch('apps.settings.redis_client.redis_client')
    def test_get_setting_value_enabled(self, mock_redis):
        mock_redis.get.return_value = '{"value": "my_val", "enabled": true}'
        val = get_setting_value("test_key")
        mock_redis.get.assert_called_with("caching_dev_setting:test_key")
        self.assertEqual(val, "my_val")

    @patch('apps.settings.redis_client.redis_client')
    def test_get_setting_value_disabled(self, mock_redis):
        mock_redis.get.return_value = '{"value": "my_val", "enabled": false}'
        val = get_setting_value("test_key")
        mock_redis.get.assert_called_with("caching_dev_setting:test_key")
        self.assertIsNone(val)

    @patch('apps.settings.redis_client.redis_client')
    def test_get_setting_value_missing(self, mock_redis):
        mock_redis.get.return_value = None
        val = get_setting_value("test_key")
        mock_redis.get.assert_called_with("caching_dev_setting:test_key")
        self.assertIsNone(val)
