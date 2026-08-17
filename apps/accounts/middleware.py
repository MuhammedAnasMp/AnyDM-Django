from django.http import JsonResponse

class SubscriptionMiddleware:
    """
    Middleware that blocks access to CRM (Inbox) and Products if the user's plan is expired.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Authenticate JWT token if present in headers (since DRF auth runs after middleware)
        if not (hasattr(request, 'user') and request.user and request.user.is_authenticated):
            auth_header = request.headers.get('Authorization')
            if auth_header and auth_header.startswith('Bearer '):
                try:
                    from rest_framework_simplejwt.authentication import JWTAuthentication
                    jwt_auth = JWTAuthentication()
                    validated_token = jwt_auth.get_validated_token(auth_header.split(' ')[1])
                    user = jwt_auth.get_user(validated_token)
                    if user:
                        request.user = user
                except Exception:
                    pass

        if hasattr(request, 'user') and request.user and request.user.is_authenticated:
            user = request.user
            from django.utils import timezone
            
            # Proactive subscription & trial expiry check and demotion
            if user.plan == 'pro' and user.premium_expires_at and user.premium_expires_at < timezone.now():
                user.plan = 'expired'
                user.save(update_fields=['plan'])
            elif user.plan == 'trial' and user.trial_start_date:
                expiry = user.trial_start_date + timezone.timedelta(days=user.trial_days)
                if timezone.now() > expiry:
                    user.plan = 'expired'
                    user.save(update_fields=['plan'])

            path = request.path
            
            # Protect CRM and Products API endpoints
            is_crm = path.startswith('/api/crm/') and 'webhooks/instagram/' not in path
            is_products = path.startswith('/api/products/') and 'resolve/' not in path
            
            if is_crm or is_products:
                if not request.user.is_premium_active:
                    return JsonResponse({
                        'error': 'Plan Expired',
                        'details': 'Your plan has expired. Please upgrade or extend your trial to access this feature.'
                    }, status=403)
                    
        return self.get_response(request)
