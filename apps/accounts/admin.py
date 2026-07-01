from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import InstagramAccount, WebsiteSettings, SystemSettings
from django.contrib.auth import get_user_model

User = get_user_model()

admin.site.register(InstagramAccount)
admin.site.register(WebsiteSettings)
admin.site.register(User)
admin.site.register(SystemSettings)
