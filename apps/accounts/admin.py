from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    InstagramAccount, 
    WebsiteSettings, 
    LinkInBioPage, 
    LinkInBioBlock, 
    LinkInBioRedirectRule, 
    LinkInBioAnalyticsEvent
)
from django.contrib.auth import get_user_model

User = get_user_model()

admin.site.register(InstagramAccount)
admin.site.register(WebsiteSettings)
admin.site.register(LinkInBioPage)
admin.site.register(LinkInBioBlock)
admin.site.register(LinkInBioRedirectRule)
admin.site.register(LinkInBioAnalyticsEvent)
admin.site.register(User)
# SystemSettings registration removed from accounts admin

