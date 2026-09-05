from django.db import models
from django.contrib.auth.models import AbstractUser
from django.conf import settings

class User(AbstractUser):
    firebase_uid = models.CharField(max_length=255, unique=True, null=True, blank=True)
    login_methods = models.JSONField(default=list)  # e.g., ["google", "email", "instagram"]
    
    # The active account working context
    active_instagram_account = models.ForeignKey(
        'InstagramAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_for_user'
    )

    # Referral & Subscription fields
    referral_code = models.CharField(max_length=50, unique=True, null=True, blank=True)
    referred_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='referrals')
    referred_by_set = models.BooleanField(default=False)
    referral_paid_reward_given = models.BooleanField(default=False)
    redeemed_months = models.IntegerField(default=0)
    is_creator_vip = models.BooleanField(default=False)
    creator_reward_type = models.CharField(
        max_length=20,
        choices=[('vip', 'VIP Free Pro'), ('commission', 'Commission Earnings')],
        null=True, blank=True, default=None
    )
    creator_commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=10.00
    )
    custom_code_set = models.BooleanField(default=False)
    pro_purchase_count = models.IntegerField(default=0)
    points = models.IntegerField(default=0)
    plan = models.CharField(max_length=20, default='trial') # 'trial', 'pro', 'expired'
    trial_days = models.IntegerField(default=14)
    trial_start_date = models.DateTimeField(null=True, blank=True)
    premium_expires_at = models.DateTimeField(null=True, blank=True)
    has_extended_trial = models.BooleanField(default=False)
    is_following_official_account = models.BooleanField(default=False)
    official_follow_points_awarded = models.IntegerField(default=0)
    official_follow_at = models.DateTimeField(null=True, blank=True)
    official_unfollow_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_premium_active(self):
        from django.utils import timezone
        if self.plan == 'pro':
            if self.premium_expires_at:
                return timezone.now() < self.premium_expires_at
            return True
        if self.trial_start_date:
            expiry = self.trial_start_date + timezone.timedelta(days=self.trial_days)
            return timezone.now() < expiry
        return False

    @property
    def trial_days_left(self):
        from django.utils import timezone
        if not self.trial_start_date:
            return 0
        expiry = self.trial_start_date + timezone.timedelta(days=self.trial_days)
        delta = expiry - timezone.now()
        return max(0, delta.days)

    def save(self, *args, **kwargs):
        import uuid
        from django.utils import timezone
        if not self.referral_code:
            self.referral_code = f"REF-{uuid.uuid4().hex[:6].upper()}"
        if not self.id and not self.trial_start_date:
            self.trial_start_date = timezone.now()
            try:
                from apps.settings.models import SystemSettings
                sys_settings = SystemSettings.get_settings()
                self.trial_days = sys_settings.trial_days
            except Exception:
                self.trial_days = 14
        super().save(*args, **kwargs)

    def __str__(self):
        return f"User: {self.username}"

    def refresh_instagram_profiles(self):
        """
        Triggers profile picture refresh for all active Instagram accounts of this user.
        """
        for account in self.instagram_accounts.filter(is_active=True):
            account.refresh_profile_picture()

class InstagramAccount(models.Model): # sellers
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name='instagram_accounts', null=True, blank=True)
    instagram_scoped_id = models.CharField(max_length=255, unique=True, null=True, blank=True) # The SID/PSID tied to the platform
    instagram_user_id = models.CharField(max_length=255, blank=True, null=True) # The global IGID (starts with 17)
    username = models.CharField(max_length=255)
    full_name = models.CharField(max_length=255, blank=True, null=True)
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True, null=True)
    profile_picture_url = models.URLField(max_length=1000, blank=True, null=True)
    used_for_login = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    is_enabled = models.BooleanField(default=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    token_refreshed_at = models.DateTimeField(null=True, blank=True)
    is_token_expired = models.BooleanField(default=False)

    def refresh_token_if_needed(self):
        """
        Automatically refreshes the long-lived access token if it has been 30 days
        since the last refresh.
        """
        from django.utils import timezone
        import requests
        
        if not self.access_token or self.is_token_expired:
            return False
            
        now = timezone.now()
        last_refreshed = self.token_refreshed_at or self.connected_at
        
        # 30 days = 30 * 86400 seconds = 2592000 seconds
        if last_refreshed and (now - last_refreshed).total_seconds() < 2592000:
            return False
            
        try:
            url = "https://graph.instagram.com/refresh_access_token"
            params = {
                "grant_type": "ig_refresh_token",
                "access_token": self.access_token
            }
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                new_token = data.get("access_token")
                if new_token:
                    self.access_token = new_token
                    self.token_refreshed_at = now
                    self.is_token_expired = False
                    self.save(update_fields=['access_token', 'token_refreshed_at', 'is_token_expired'])
                    return True
            elif response.status_code in [400, 401, 403]:
                self.is_token_expired = True
                self.save(update_fields=['is_token_expired'])
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error refreshing Instagram access token for {self.username}: {e}")
            
        return False

    def refresh_profile_picture(self):
        """
        Refreshes the profile picture URL from the Instagram Graph API.
        Only performs the refresh if it hasn't been refreshed in the last 24 hours.
        """
        from django.utils import timezone
        import requests
        
        now = timezone.now()
        if self.last_refreshed_at and (now - self.last_refreshed_at).total_seconds() < 86400:
            return False
            
        if not self.access_token:
            return False
            
        try:
            response = requests.get(
                "https://graph.instagram.com/v26.0/me",
                params={
                    'fields': 'profile_picture_url',
                    'access_token': self.access_token
                },
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                profile_pic = data.get('profile_picture_url')
                if profile_pic:
                    self.profile_picture_url = profile_pic
                self.last_refreshed_at = now
                self.save(update_fields=['profile_picture_url', 'last_refreshed_at'])
                print("prrrrrrrrrrrrrrroooooooooooooooooooofffffffffffffffiiiiiiiiiiiiilllllllllleeeeeeeeeeeeeeeeeeeeeeeeee")
                return True
            elif response.status_code in [400, 401, 403]:
                self.is_token_expired = True
                self.save(update_fields=['is_token_expired'])
        except Exception as e:
            # Import logging and log the error to avoid cluttering stdout but keep it debuggable
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error refreshing profile picture for {self.username}: {e}")
            
        return False

    def __str__(self):
        return f"{self.username} ({self.instagram_user_id})"


class WebsiteSettings(models.Model):
    instagram_account = models.OneToOneField(
        InstagramAccount,
        on_delete=models.CASCADE,
        related_name='website_settings'
    )
    store_name = models.CharField(max_length=255, blank=True, null=True)
    store_logo = models.URLField(max_length=2000, blank=True, null=True)
    
    # Store settings
    store_slug = models.CharField(max_length=255, unique=True, null=True, blank=True)
    custom_domain = models.CharField(max_length=255, unique=True, null=True, blank=True)
    store_banner = models.URLField(max_length=2000, blank=True, null=True)
    store_description = models.TextField(blank=True, null=True)
    contact_email = models.CharField(max_length=255, blank=True, null=True)
    contact_phone = models.CharField(max_length=255, blank=True, null=True)
    business_address = models.TextField(blank=True, null=True)
    shipping_address = models.TextField(blank=True, null=True)
    return_policy = models.BooleanField(default=False)
    cancellation_policy = models.BooleanField(default=False)
    cod_enabled = models.BooleanField(default=True)
    online_payment_enabled = models.BooleanField(default=True)

    # Product Display Settings
    show_related_products = models.BooleanField(default=True)
    
    # Purchase Actions
    enable_instagram_button = models.BooleanField(default=True)
    enable_whatsapp_button = models.BooleanField(default=True)
    
    # Appearance Settings
    template_id = models.CharField(max_length=100, default='glass_monochrome')
    theme_id = models.CharField(max_length=100, default='dark')
    
    privacy_policy = models.TextField(blank=True, null=True)
    terms_of_service = models.TextField(blank=True, null=True)

    # Extensible custom fields
    custom_colors = models.JSONField(default=dict, blank=True)
    custom_fonts = models.JSONField(default=dict, blank=True)
    custom_settings = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"WebsiteSettings for {self.instagram_account.username}"


class SellerKYC(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending Verification'),
        ('SUBMITTED', 'Documents Submitted'),
        ('REVIEW', 'Under Review'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('SUSPENDED', 'Suspended'),
    ]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='kyc')
    full_name = models.CharField(max_length=255, blank=True, null=True)
    pan_number = models.CharField(max_length=50, blank=True, null=True)
    aadhaar_number = models.CharField(max_length=50, blank=True, null=True)
    bank_name = models.CharField(max_length=255, blank=True, null=True)
    bank_account_number = models.CharField(max_length=100, blank=True, null=True)
    bank_ifsc = models.CharField(max_length=50, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    is_card_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"KYC for {self.user.username} - {self.status}"



class CreatorCommission(models.Model):
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='commissions_earned')
    referred_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='commission_generated')
    payment_amount = models.DecimalField(max_digits=10, decimal_places=2)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, default='pending', choices=[('pending', 'Pending'), ('paid', 'Paid')])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Commission: {self.creator.username} earned {self.commission_amount} from {self.referred_user.username}"


# SystemSettings has been moved to apps.settings.models

