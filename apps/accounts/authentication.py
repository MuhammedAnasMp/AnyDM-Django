from rest_framework_simplejwt.authentication import JWTAuthentication
import logging

logger = logging.getLogger(__name__)

class CustomJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        user_auth_tuple = super().authenticate(request)
        if user_auth_tuple is not None:
            user, token = user_auth_tuple
            try:
                user.refresh_instagram_profiles()
            except Exception as e:
                logger.error(f"Error refreshing Instagram profiles during authentication: {e}")
        return user_auth_tuple
