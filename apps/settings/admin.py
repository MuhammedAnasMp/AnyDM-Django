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
    fieldsets = (
        ('Pricing & Trial', {
            'fields': ('premium_plan_price', 'trial_days', 'extend_days')
        }),
        ('Referral & Points', {
            'fields': ('referral_points', 'points_to_redeem')
        }),
        ('Official Instagram Follow Reward', {
            'fields': ('official_follow_points',),
            'description': 'Configure the follow reward points.'
        }),
        ('AI Features', {
            'fields': ('enable_ai', 'enable_subscription_ai', 'business_gemini_api_key')
        }),
        ('Payout & Automated Transfer Integration', {
            'fields': ('enable_razorpay_route', 'enable_razorpay_payouts_api', 'manual_settlement_notes'),
            'description': 'Enable Razorpay Route marketplace split or RazorpayX Payouts API once approved on your account.'
        }),
    )

    def has_add_permission(self, request):
        if SystemSettings.objects.exists():
            return False
        return True

    def has_delete_permission(self, request, obj=None):
        return False
