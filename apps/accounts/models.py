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
                "https://graph.instagram.com/v25.0/me",
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
    
    # Product Display Settings
    show_related_products = models.BooleanField(default=True)
    
    # Purchase Actions
    enable_instagram_button = models.BooleanField(default=True)
    enable_whatsapp_button = models.BooleanField(default=True)
    
    # Appearance Settings
    template_id = models.CharField(max_length=100, default='glass_monochrome')
    theme_id = models.CharField(max_length=100, default='dark')
    
    # Extensible custom fields
    custom_colors = models.JSONField(default=dict, blank=True)
    custom_fonts = models.JSONField(default=dict, blank=True)
    custom_settings = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"WebsiteSettings for {self.instagram_account.username}"
