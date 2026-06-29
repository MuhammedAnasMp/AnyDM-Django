from django.contrib import admin
from .models import (
    AutomationCampaign,
    AutomationRule,
    AutomationAction,
    GiveawayConfig,
    GiveawayReward,
    AutomationEngagement,
    AutomationWinner,
    AutomationExecution,
)


class AutomationActionInline(admin.TabularInline):
    model = AutomationAction
    extra = 1
    fields = ('order', 'action_type', 'dm_format', 'message_mode', 'messages')


class GiveawayConfigInline(admin.StackedInline):
    model = GiveawayConfig
    extra = 0
    max_num = 1


class GiveawayRewardInline(admin.TabularInline):
    model = GiveawayReward
    fk_name = 'giveaway'
    extra = 1
    fields = ('reward_id', 'reward_type', 'value', 'quantity', 'remaining')


@admin.register(AutomationCampaign)
class AutomationCampaignAdmin(admin.ModelAdmin):
    list_display  = ('name', 'campaign_id', 'seller', 'status', 'start_at', 'end_at')
    list_filter   = ('status',)
    search_fields = ('name', 'campaign_id')


@admin.register(AutomationRule)
class AutomationRuleAdmin(admin.ModelAdmin):
    list_display  = ('name', 'rule_type', 'status', 'seller')
    list_filter   = ('rule_type', 'status')
    search_fields = ('name',)
    ordering      = ('-id',)
    inlines       = [AutomationActionInline, GiveawayConfigInline]


@admin.register(GiveawayConfig)
class GiveawayConfigAdmin(admin.ModelAdmin):
    list_display = ('rule', 'selection_method', 'winner_count', 'finalize_at', 'anti_fraud_enabled')
    inlines      = [GiveawayRewardInline]


@admin.register(AutomationEngagement)
class AutomationEngagementAdmin(admin.ModelAdmin):
    list_display  = ('customer', 'rule', 'score', 'comment_count', 'is_eligible', 'engaged_at')
    list_filter   = ('is_eligible', 'rule')
    search_fields = ('customer__username',)


@admin.register(AutomationWinner)
class AutomationWinnerAdmin(admin.ModelAdmin):
    list_display  = ('rank', 'customer', 'rule', 'reward_value', 'notification_status', 'is_claimed')
    list_filter   = ('notification_status', 'is_claimed')
    search_fields = ('customer__username',)


@admin.register(AutomationExecution)
class AutomationExecutionAdmin(admin.ModelAdmin):
    list_display  = ('rule', 'customer', 'trigger_event_type', 'status', 'executed_at')
    list_filter   = ('status', 'trigger_event_type')
    search_fields = ('trigger_text', 'trigger_media_id')
    readonly_fields = ('trigger_event_hash', 'actions_log', 'executed_at')
