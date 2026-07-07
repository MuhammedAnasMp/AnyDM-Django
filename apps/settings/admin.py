from django.contrib import admin
from apps.settings.models import CachingDevSetting


@admin.register(CachingDevSetting)
class CachingDevSettingAdmin(admin.ModelAdmin):
    list_display = ('key', 'value', 'enabled', 'created_at', 'updated_at')
    search_fields = ('key', 'value')
    list_filter = ('enabled', 'created_at', 'updated_at')
    ordering = ('key',)
