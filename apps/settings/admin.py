from django.contrib import admin
from apps.settings.models import CachingDevSetting, SystemSettings


@admin.register(CachingDevSetting)
class CachingDevSettingAdmin(admin.ModelAdmin):
    list_display = ('key', 'value', 'enabled', 'created_at', 'updated_at')
    search_fields = ('key', 'value')
    list_filter = ('enabled', 'created_at', 'updated_at')
    ordering = ('key',)


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ('premium_plan_price', 'trial_days', 'enable_ai', 'enable_subscription_ai', 'updated_at')
    fields = ('premium_plan_price', 'trial_days', 'extend_days', 'referral_points', 'points_to_redeem', 'enable_ai', 'enable_subscription_ai', 'business_gemini_api_key')

    def has_add_permission(self, request):
        if SystemSettings.objects.exists():
            return False
        return True

    def has_delete_permission(self, request, obj=None):
        return False
