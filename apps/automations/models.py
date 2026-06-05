from django.db import models, transaction
from django.core.exceptions import ValidationError


# ─────────────────────────────────────────────────────────────────────────────
# CAMPAIGN WRAPPER
# Groups multiple automation rules under one campaign
# ─────────────────────────────────────────────────────────────────────────────

class AutomationCampaign(models.Model):
    STATUS = [
        ('draft',     'Draft'),
        ('active',    'Active'),
        ('paused',    'Paused'),
        ('completed', 'Completed'),
        ('archived',  'Archived'),
    ]

    seller      = models.ForeignKey(
        'accounts.InstagramAccount',
        on_delete=models.CASCADE,
        related_name='campaigns'
    )
    name        = models.CharField(max_length=255)
    campaign_id = models.SlugField(max_length=100, unique=True,
                                   help_text="Human-readable unique ID e.g. summer-sale-2026")
    status      = models.CharField(max_length=20, choices=STATUS, default='draft')
    timezone    = models.CharField(max_length=50, default='UTC',
                                   help_text="IANA timezone e.g. Africa/Cairo, UTC+3")

    global_rate_limit = models.JSONField(
        default=dict, blank=True,
        help_text='{"scope": "global", "limit": 1000, "window_seconds": 86400}'
    )

    start_at   = models.DateTimeField(null=True, blank=True)
    end_at     = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"[Campaign] {self.name}"

    class Meta:
        ordering = ['-created_at']


# ─────────────────────────────────────────────────────────────────────────────
# RULE TYPE → TRIGGER EVENT (strict 1:1 mapping)
#
#   comment_automation        → comment_event
#   story_automation          → story_reply
#   dm_automation             → dm_event
#   giveaway_comment          → comment_event
#   giveaway_dm               → dm_event
#   product_inquiry_comment   → comment_event
#   product_inquiry_story     → story_reply
#   product_inquiry_dm        → dm_event
# ─────────────────────────────────────────────────────────────────────────────

RULE_TYPE_TO_TRIGGER = {
    'comment_automation':      'comment_event',
    'story_automation':        'story_reply',
    'dm_automation':           'dm_event',
    'giveaway_comment':        'comment_event',
    'giveaway_dm':             'dm_event',
    'product_inquiry_comment': 'comment_event',
    'product_inquiry_story':   'story_reply',
    'product_inquiry_dm':      'dm_event',
}


# ─────────────────────────────────────────────────────────────────────────────
# AUTOMATION RULE
# ─────────────────────────────────────────────────────────────────────────────

class AutomationRule(models.Model):

    RULE_TYPES = [
        ('comment_automation',      'Comment Automation        → comment_event'),
        ('story_automation',        'Story Automation          → story_reply'),
        ('dm_automation',           'DM Automation             → dm_event'),
        ('giveaway_comment',        'Giveaway (Comment)        → comment_event'),
        ('giveaway_dm',             'Giveaway (DM)             → dm_event'),
        ('product_inquiry_comment', 'Product Inquiry (Comment) → comment_event'),
        ('product_inquiry_story',   'Product Inquiry (Story)   → story_reply'),
        ('product_inquiry_dm',      'Product Inquiry (DM)      → dm_event'),
    ]

    STATUS = [
        ('draft',     'Draft'),
        ('active',    'Active'),
        ('paused',    'Paused'),
        ('completed', 'Completed'),
    ]

    TARGET_MODES = [
        ('every',    'Every Post / Story'),
        ('selected', 'Selected Media Only'),
    ]

    MATCH_TYPES = [
        ('contains', 'Contains Keyword'),
        ('equals',   'Exact Match'),
        ('any',      'Any Message'),
    ]

    # ── Ownership ────────────────────────────────────────────────────────────
    seller   = models.ForeignKey(
        'accounts.InstagramAccount',
        on_delete=models.CASCADE,
        related_name='automation_rules'
    )
    campaign = models.ForeignKey(
        AutomationCampaign,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='rules'
    )

    # ── Identity ─────────────────────────────────────────────────────────────
    name      = models.CharField(max_length=255)
    rule_type = models.CharField(max_length=40, choices=RULE_TYPES)
    status    = models.CharField(max_length=20, choices=STATUS, default='draft')

    # ── Priority (1 = highest, 100 = lowest) ─────────────────────────────────
    priority      = models.PositiveSmallIntegerField(default=50)
    stop_on_match = models.BooleanField(
        default=True,
        help_text="Stop evaluating lower-priority rules once this one matches"
    )

    # ── Target ───────────────────────────────────────────────────────────────
    target_mode       = models.CharField(max_length=20, choices=TARGET_MODES, default='every')
    target_media_ids  = models.JSONField(default=list, blank=True)
    target_media_type = models.CharField(max_length=30, blank=True, null=True,
                                         help_text="post | reel | story | reel_or_post")

    # ── Condition ────────────────────────────────────────────────────────────
    condition_match_type = models.CharField(max_length=20, choices=MATCH_TYPES, default='contains')
    condition_keywords   = models.JSONField(default=list, blank=True)

    # ── Deduplication ────────────────────────────────────────────────────────
    deduplication_enabled        = models.BooleanField(default=True)
    deduplication_window_seconds = models.PositiveIntegerField(default=86400)
    deduplication_unique_per     = models.JSONField(
        default=list,
        help_text='["user_id", "rule_id"] or ["user_id", "media_id"]'
    )

    # ── Follower gate ─────────────────────────────────────────────────────────
    follower_gate_enabled  = models.BooleanField(default=False)
    follower_gate_messages = models.JSONField(default=list, blank=True)

    # ── Schedule ──────────────────────────────────────────────────────────────
    start_at = models.DateTimeField(null=True, blank=True)
    end_at   = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def trigger_event(self):
        """Derived from rule_type. Never stored separately."""
        return RULE_TYPE_TO_TRIGGER.get(self.rule_type)

    def __str__(self):
        return f"[{self.get_rule_type_display()}] {self.name} (priority={self.priority})"

    class Meta:
        ordering = ['priority', '-created_at']


# ─────────────────────────────────────────────────────────────────────────────
# AUTOMATION ACTION
# ─────────────────────────────────────────────────────────────────────────────

class AutomationAction(models.Model):

    ACTION_TYPES = [
        ('reply_comment', 'Reply to Comment'),
        ('reply_story',   'Reply to Story'),
        ('send_dm',       'Send Direct Message'),
    ]

    DM_FORMATS = [
        ('text',             'Plain Text'),
        ('quick_reply',      'Quick Reply Buttons'),
        ('generic_template', 'Carousel / Generic Template'),
        ('button_template',  'Button Template'),
    ]

    MESSAGE_MODES = [
        ('random',     'Random from list'),
        ('sequential', 'One by one in order'),
        ('fixed',      'Always first message'),
    ]

    rule         = models.ForeignKey(AutomationRule, on_delete=models.CASCADE, related_name='actions')
    order        = models.PositiveSmallIntegerField(default=0)
    action_type  = models.CharField(max_length=30, choices=ACTION_TYPES)
    message_mode = models.CharField(max_length=20, choices=MESSAGE_MODES, default='random')
    messages     = models.JSONField(default=list)

    # ── DM format ─────────────────────────────────────────────────────────────
    dm_format = models.CharField(max_length=30, choices=DM_FORMATS, default='text', blank=True)

    # ── DM payloads ───────────────────────────────────────────────────────────
    quick_reply_payload      = models.JSONField(default=dict, blank=True)
    generic_template_payload = models.JSONField(default=dict, blank=True)
    button_template_payload  = models.JSONField(default=dict, blank=True)

    # ── Product link (for product_inquiry rules) ──────────────────────────────
    linked_product = models.ForeignKey(
        'products.Product',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='automation_actions'
    )

    # ── Rate limit ────────────────────────────────────────────────────────────
    rate_limit = models.JSONField(
        default=dict,
        help_text='{"scope": "user", "limit": 1, "window_seconds": 86400}'
    )

    # ── Error handling ─────────────────────────────────────────────────────────
    on_fail = models.JSONField(
        default=dict, blank=True,
        help_text='{"retry": 2, "retry_delay_seconds": 30, "fallback_action": "reply_comment", "fallback_messages": []}'
    )

    # ── URL security ──────────────────────────────────────────────────────────
    url_allowlist = models.JSONField(default=list, blank=True,
                                     help_text='["shop.example.com"]')

    # ── Payload validation ────────────────────────────────────────────────────
    def clean(self):
        if self.action_type != 'send_dm':
            return

        if self.dm_format == 'quick_reply':
            qr = self.quick_reply_payload
            if not isinstance(qr, dict) or 'quick_replies' not in qr:
                raise ValidationError(
                    {'quick_reply_payload': 'Must be a dict with a "quick_replies" list.'}
                )
            for item in qr.get('quick_replies', []):
                if not isinstance(item, dict) or 'content_type' not in item:
                    raise ValidationError(
                        {'quick_reply_payload': 'Each quick_reply must have "content_type".'}
                    )

        elif self.dm_format == 'generic_template':
            gt = self.generic_template_payload
            if not isinstance(gt, dict) or not gt.get('elements'):
                raise ValidationError(
                    {'generic_template_payload': 'Must be a dict with a non-empty "elements" list.'}
                )
            for el in gt.get('elements', []):
                if not isinstance(el, dict) or 'title' not in el:
                    raise ValidationError(
                        {'generic_template_payload': 'Each element must have a "title" field.'}
                    )

        elif self.dm_format == 'button_template':
            bt = self.button_template_payload
            if not isinstance(bt, dict) or not bt.get('buttons'):
                raise ValidationError(
                    {'button_template_payload': 'Must be a dict with a non-empty "buttons" list.'}
                )
            for btn in bt.get('buttons', []):
                if btn.get('type') not in ('web_url', 'postback'):
                    raise ValidationError(
                        {'button_template_payload': 'Button type must be "web_url" or "postback".'}
                    )
                if btn['type'] == 'web_url' and not btn.get('url'):
                    raise ValidationError(
                        {'button_template_payload': 'web_url buttons must include a "url".'}
                    )
                if btn['type'] == 'postback' and not btn.get('payload'):
                    raise ValidationError(
                        {'button_template_payload': 'postback buttons must include a "payload".'}
                    )

        if not self.messages or not isinstance(self.messages, list):
            raise ValidationError({'messages': 'At least one message string is required.'})

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.rule.name} → Step {self.order}: {self.get_action_type_display()}"


# ─────────────────────────────────────────────────────────────────────────────
# GIVEAWAY CONFIG
# ─────────────────────────────────────────────────────────────────────────────

class GiveawayConfig(models.Model):

    SELECTION_METHODS = [
        ('random',                 'Random — Equal chance'),
        ('most_engaged',           'Most Engaged User'),
        ('ai_ranked',              'AI Ranked'),
        ('most_comment_activity',  'Most Comment Activity'),
        ('first_come_first_serve', 'First Engager'),
        ('weighted_random',        'Weighted Random (Hybrid)'),
    ]

    rule             = models.OneToOneField(AutomationRule, on_delete=models.CASCADE, related_name='giveaway_config')
    selection_method = models.CharField(max_length=40, choices=SELECTION_METHODS, default='random')
    winner_count     = models.PositiveIntegerField(default=1)

    scoring_weights = models.JSONField(default=dict, blank=True)
    ai_model        = models.CharField(max_length=100, blank=True, default='engagement_ranker_v1')
    ai_factors      = models.JSONField(default=list, blank=True)

    # ── Execution timing ──────────────────────────────────────────────────────
    evaluation_window_seconds = models.PositiveIntegerField(default=604800)
    finalize_at               = models.DateTimeField(null=True, blank=True)
    re_evaluation_allowed     = models.BooleanField(default=False)
    re_evaluation_max         = models.PositiveSmallIntegerField(default=0)
    data_snapshot_at          = models.DateTimeField(null=True, blank=True)

    # ── Anti-fraud ────────────────────────────────────────────────────────────
    anti_fraud_enabled = models.BooleanField(default=True)
    anti_fraud_filters = models.JSONField(
        default=dict, blank=True,
        help_text='{"min_account_age_days": 30, "min_followers": 0, "block_duplicate_ips": true}'
    )

    # ── Gamification / Spin wheel ─────────────────────────────────────────────
    gamification_enabled    = models.BooleanField(default=False)
    spin_wheel_enabled      = models.BooleanField(default=False)
    spin_wheel_base_url     = models.URLField(blank=True, null=True)
    spin_wheel_single_use   = models.BooleanField(default=True)
    spin_wheel_url_mode     = models.CharField(max_length=30, default='signed_token')
    spin_token_expiry_hours = models.PositiveIntegerField(default=48)

    reward_delivery_methods = models.JSONField(
        default=list, blank=True,
        help_text='["direct_dm", "instagram_story", "public_post_announcement"]'
    )

    def __str__(self):
        return f"Giveaway Config → {self.rule.name} ({self.get_selection_method_display()})"


class GiveawayReward(models.Model):

    REWARD_TYPES = [
        ('discount', 'Discount Code'),
        ('physical', 'Physical Product'),
        ('mystery',  'Mystery Box'),
        ('digital',  'Digital Download'),
        ('custom',   'Custom'),
    ]

    giveaway    = models.ForeignKey(GiveawayConfig, on_delete=models.CASCADE, related_name='reward_pool')
    reward_id   = models.CharField(max_length=50)
    reward_type = models.CharField(max_length=30, choices=REWARD_TYPES)
    value       = models.CharField(max_length=255)
    quantity    = models.PositiveIntegerField(default=1)
    remaining   = models.PositiveIntegerField(default=1)

    def claim_one(self):
        """
        Atomically decrements `remaining` by 1.
        Uses select_for_update() to prevent race conditions.
        Raises ValueError if inventory is exhausted.
        """
        with transaction.atomic():
            locked = GiveawayReward.objects.select_for_update().get(pk=self.pk)
            if locked.remaining < 1:
                raise ValueError(f"Reward '{locked.value}' is fully claimed.")
            locked.remaining -= 1
            locked.save(update_fields=['remaining'])
        return True

    def clean(self):
        if self.remaining > self.quantity:
            raise ValidationError({'remaining': '`remaining` cannot exceed `quantity`.'})

    def __str__(self):
        return f"{self.get_reward_type_display()}: {self.value} ({self.remaining}/{self.quantity} left)"


# ─────────────────────────────────────────────────────────────────────────────
# ENGAGEMENT TRACKING
# ─────────────────────────────────────────────────────────────────────────────

class AutomationEngagement(models.Model):

    rule = models.ForeignKey(AutomationRule, on_delete=models.CASCADE, related_name='engagements')

    customer = models.ForeignKey(
        'crm.Customer',
        on_delete=models.CASCADE,
        related_name='automation_engagements'
    )

    source_interaction = models.ForeignKey(
        'crm.CustomerInteraction',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='automation_engagements',
        help_text="The exact comment/DM/story reply that triggered this"
    )

    score         = models.FloatField(default=0.0)
    comment_count = models.PositiveIntegerField(default=0)
    is_eligible   = models.BooleanField(default=True)

    spin_token_issued = models.BooleanField(default=False)
    spin_used         = models.BooleanField(default=False)

    metadata   = models.JSONField(default=dict, blank=True)
    engaged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Per-interaction uniqueness — allows multi-entry giveaways & comment marathons
        unique_together = ('rule', 'customer', 'source_interaction')
        ordering = ['-engaged_at']

    def __str__(self):
        return f"{self.customer} → {self.rule.name} (score={self.score})"


# ─────────────────────────────────────────────────────────────────────────────
# WINNER STORAGE
# ─────────────────────────────────────────────────────────────────────────────

class AutomationWinner(models.Model):

    NOTIFICATION_STATUS = [
        ('pending',  'Pending'),
        ('notified', 'Notified'),
        ('failed',   'Failed'),
    ]

    rule       = models.ForeignKey(AutomationRule, on_delete=models.CASCADE, related_name='winners')
    customer   = models.ForeignKey('crm.Customer', on_delete=models.CASCADE, related_name='giveaway_wins')
    engagement = models.ForeignKey(AutomationEngagement, on_delete=models.SET_NULL, null=True, blank=True, related_name='winner_record')
    reward     = models.ForeignKey(GiveawayReward, on_delete=models.SET_NULL, null=True, blank=True, related_name='awarded_to')

    rank         = models.PositiveIntegerField(default=1)
    reward_type  = models.CharField(max_length=100, blank=True)
    reward_value = models.CharField(max_length=255, blank=True)

    # ── Spin wheel ────────────────────────────────────────────────────────────
    spin_token      = models.CharField(max_length=512, blank=True, null=True, unique=True)
    spin_token_used = models.BooleanField(default=False)
    spin_result     = models.JSONField(default=dict, blank=True)

    # ── Notification ──────────────────────────────────────────────────────────
    notification_status = models.CharField(max_length=20, choices=NOTIFICATION_STATUS, default='pending')
    notified_via        = models.JSONField(default=list, blank=True,
                                           help_text='["direct_dm", "instagram_story", "public_post"]')
    notified_at         = models.DateTimeField(null=True, blank=True)

    # ── Claim ─────────────────────────────────────────────────────────────────
    is_claimed = models.BooleanField(default=False)
    claimed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['rank', 'created_at']

    def __str__(self):
        return f"Winner #{self.rank}: {self.customer} — {self.rule.name}"


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTION LOG
# ─────────────────────────────────────────────────────────────────────────────

class AutomationExecution(models.Model):

    STATUSES = [
        ('success',      'Success'),
        ('failed',       'Failed'),
        ('rate_limited', 'Rate Limited'),
        ('deduplicated', 'Skipped — Deduplication'),
        ('skipped',      'Skipped — Condition Not Met'),
        ('partial',      'Partial — Some Actions Failed'),
        ('fallback',     'Fallback Action Used'),
    ]

    rule     = models.ForeignKey(AutomationRule, on_delete=models.CASCADE, related_name='executions')
    customer = models.ForeignKey('crm.Customer', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='automation_executions')

    trigger_event_type = models.CharField(max_length=30,
                                          help_text="comment_event | story_reply | dm_event")
    trigger_text       = models.TextField(blank=True, null=True)
    trigger_media_id   = models.CharField(max_length=255, blank=True, null=True)

    # ── Idempotency ───────────────────────────────────────────────────────────
    # sha256(f"{rule.id}:{instagram_event_id}") — set by the processing engine
    trigger_event_hash = models.CharField(
        max_length=64, blank=True, null=True, db_index=True,
        help_text="SHA-256 of (rule_id + instagram_event_id). Prevents duplicate processing."
    )

    status        = models.CharField(max_length=20, choices=STATUSES, default='success')
    actions_log   = models.JSONField(default=list, blank=True,
                                     help_text='[{action_type, status, message_sent, retry_count, error}]')
    error_message = models.TextField(blank=True, null=True)

    executed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-executed_at']
        constraints = [
            models.UniqueConstraint(
                fields=['rule', 'trigger_event_hash'],
                condition=models.Q(trigger_event_hash__isnull=False),
                name='unique_rule_event_hash'
            )
        ]

    def __str__(self):
        return f"{self.rule.name} | {self.status} | {self.executed_at:%Y-%m-%d %H:%M}"
