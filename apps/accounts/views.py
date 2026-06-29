import requests
import base64
import hashlib
import hmac
import json
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.permissions import IsAuthenticated, AllowAny
from .firebase_auth import verify_firebase_token, delete_firebase_user
from .models import InstagramAccount, WebsiteSettings
from apps.products.models import Product
from django.contrib.auth import get_user_model
from django.conf import settings
from django.utils import timezone
User = get_user_model()

def parse_signed_request(signed_request):
    try:
        encoded_sig, payload = signed_request.split('.', 2)
        sig = base64.urlsafe_b64decode(encoded_sig + '=' * (4 - len(encoded_sig) % 4))
        data = json.loads(base64.urlsafe_b64decode(payload + '=' * (4 - len(payload) % 4)).decode('utf-8'))
        
        # Verify signature
        expected_sig = hmac.new(
            settings.INSTAGRAM_CLIENT_SECRET.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        if sig != expected_sig:
            return None
        return data
    except Exception as e:
        print(f"Error parsing signed request: {e}")
        return None

def get_tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }

class FirebaseLoginView(APIView):
    def post(self, request):
        id_token = request.data.get('id_token')
        if not id_token:
            return Response({'error': 'ID token is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        decoded_token = verify_firebase_token(id_token)
        if not decoded_token:
            return Response({'error': 'Invalid ID token'}, status=status.HTTP_401_UNAUTHORIZED)
        
        uid = decoded_token.get('uid')
        email = decoded_token.get('email')
        name = decoded_token.get('name', '')
        
        try:
            if not email:
                # Fallback to uid if email is missing (e.g. anonymous or phone login)
                email = f"{uid}@anydm.internal"

            # 1. Try to resolve by firebase_uid (Most reliable)
            user = User.objects.filter(firebase_uid=uid).first()
            
            # 2. If not found, try by email (Merging case)
            if not user:
                user = User.objects.filter(email=email).first()

            if not user:
                # 3. Create new if absolutely no match
                user = User.objects.create(
                    username=uid, 
                    email=email, 
                    first_name=name, 
                    firebase_uid=uid
                )
                print(f"[FirebaseLogin] Created new user: {user.username}")
            else:
                # Sync info
                if not user.firebase_uid:
                    user.firebase_uid = uid
                if not user.first_name and name:
                    user.first_name = name
                user.save()
                print(f"[FirebaseLogin] Found existing user: {user.username}")
            
            # ── Resolve login methods from Firebase Admin ────────────────────────────
            from firebase_admin import auth as admin_auth
            try:
                firebase_user = admin_auth.get_user(uid)
                provider_ids = [p.provider_id for p in firebase_user.provider_data]
            except Exception as e:
                print(f"Firebase Admin Error: {e}")
                provider_ids = []
    
            provider_map = {'google.com': 'google', 'password': 'email', 'firebase': 'email'}
            firebase_methods = []
            for pid in provider_ids:
                method = provider_map.get(pid)
                if method and method not in firebase_methods:
                    firebase_methods.append(method)
    
            # Ensure user.login_methods is a list
            stored_methods = user.login_methods if isinstance(user.login_methods, list) else []
            merged_methods = list(set(stored_methods) | set(firebase_methods))
    
            if set(stored_methods) != set(merged_methods):
                user.login_methods = merged_methods

            user.last_login = timezone.now()
            user.save()
    
            # Load Instagram accounts
            instagram_accounts = InstagramAccount.objects.filter(user=user)
    
            # Generate JWT tokens
            tokens = get_tokens_for_user(user)
    
            return Response({
                'message': 'Login successful',
                'tokens': tokens,
                'user': {
                    'id': user.id,
                    'email': user.email,
                    'login_methods': merged_methods,
                    'display_name': user.first_name or user.username,
                    'active_instagram_account_id': user.active_instagram_account_id,
                },
                'instagram_accounts': [
                    {
                        'id': acc.id,
                        'username': acc.username,
                        'profile_picture_url': acc.profile_picture_url,
                        'used_for_login': acc.used_for_login,
                        'is_active': acc.is_active,
                        'is_enabled': acc.is_enabled,
                    } for acc in instagram_accounts if acc.is_active
                ]
            }, status=status.HTTP_200_OK)
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"Error in FirebaseLoginView:\n{error_trace}")
            return Response({
                'error': str(e),
                'trace': error_trace if settings.DEBUG else None
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def exchange_short_lived_for_long_lived_token(short_lived_token):
    from django.conf import settings
    # Check if it is a Basic Display token (IGAA...)
    if short_lived_token.startswith("IGAA"):
        url = "https://graph.instagram.com/access_token"
        params = {
            "grant_type": "ig_exchange_token",
            "client_secret": settings.INSTAGRAM_CLIENT_SECRET,
            "access_token": short_lived_token
        }
        try:
            r = requests.get(url, params=params)
            r.raise_for_status()
            return r.json().get("access_token", short_lived_token)
        except Exception as e:
            print(f"Error exchanging personal IG token: {e}")
            return short_lived_token
    else:
        # Standard professional Facebook Graph Exchange
        url = "https://graph.facebook.com/v25.0/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": settings.INSTAGRAM_CLIENT_ID,
            "client_secret": settings.INSTAGRAM_CLIENT_SECRET,
            "fb_exchange_token": short_lived_token
        }
        try:
            r = requests.get(url, params=params)
            r.raise_for_status()
            return r.json().get("access_token", short_lived_token)
        except Exception as e:
            print(f"Error exchanging professional IG token: {e}")
            return short_lived_token

class InstagramLoginView(APIView):
    def post(self, request):
        access_token = request.data.get('access_token')
        code = request.data.get('code')
        redirect_uri = request.data.get('redirect_uri')
        
        from django.conf import settings

        # If code is provided, exchange it for an access token
        if code and not access_token:
            exchange_url = "https://api.instagram.com/oauth/access_token"
            exchange_data = {
                'client_id': settings.INSTAGRAM_CLIENT_ID,
                'client_secret': settings.INSTAGRAM_CLIENT_SECRET,
                'grant_type': 'authorization_code',
                'redirect_uri': redirect_uri,
                'code': code,
            }
            exchange_response = requests.post(exchange_url, data=exchange_data)
            
            if exchange_response.status_code != 200:
                return Response({
                    'error': 'Failed to exchange code', 
                    'details': exchange_response.json()
                }, status=status.HTTP_401_UNAUTHORIZED)
            
            access_token = exchange_response.json().get('access_token')

        if not access_token:
            return Response({'error': 'access_token or code is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Exchange short-lived token for long-lived token (60 days)
        access_token = exchange_short_lived_for_long_lived_token(access_token)

        try:
            # Verify with Instagram using v25.0
            # 'id' is the Instagram-Scoped ID (IGSID/SID)
            # 'user_id' is the global Instagram ID (IGID, starts with 17) - requires instagram_graph_user_id permission
            response = requests.get(
                "https://graph.instagram.com/v25.0/me",
                params={
                    'fields': 'id,user_id,username,name,account_type,profile_picture_url',
                    'access_token': access_token
                }
            )
            
            if response.status_code != 200:
                return Response({'error': 'Invalid Instagram token', 'details': response.json()}, status=status.HTTP_401_UNAUTHORIZED)
            
            data = response.json()
            ig_sid = data.get('id')        # Scoped ID (PSID/SID) or actual global User ID if Basic Display API
            ig_id = data.get('user_id')    # Global ID (starts with 17) or None if Basic Display API
            ig_username = data.get('username')
            ig_full_name = data.get('name')
            ig_profile_pic = data.get('profile_picture_url')
            
            # Determine correct IDs
            # If ig_id is present, then ig_sid is the scoped ID, and ig_id is the user ID.
            # If ig_id is not present, then ig_sid itself is the global user ID!
            resolved_scoped_id = None
            resolved_user_id = None

            if ig_id:
                resolved_scoped_id = ig_sid
                resolved_user_id = ig_id
            else:
                resolved_user_id = ig_sid

            auth_header = request.headers.get('Authorization', 'No Header')
            print(f"[InstagramLogin] Auth Header: {auth_header}")
            print(f"[InstagramLogin] request.user.is_authenticated: {request.user.is_authenticated}")
            
            # Look up existing account using all possible ID permutations to avoid duplicates/login time problems
            ig_account = None
            if resolved_user_id:
                ig_account = InstagramAccount.objects.filter(instagram_user_id=resolved_user_id).first()
            if not ig_account and resolved_scoped_id:
                ig_account = InstagramAccount.objects.filter(instagram_scoped_id=resolved_scoped_id).first()
            if not ig_account and resolved_scoped_id:
                ig_account = InstagramAccount.objects.filter(instagram_user_id=resolved_scoped_id).first()
            if not ig_account and resolved_user_id:
                ig_account = InstagramAccount.objects.filter(instagram_scoped_id=resolved_user_id).first()

            if request.user.is_authenticated:
                # 1. Linking Mode (Logged in)
                user = request.user
                print(f"[InstagramLogin] Authenticated Link: User(id={user.id}, email={user.email})")

                if ig_account and ig_account.user and ig_account.user != user and ig_account.is_active:
                    return Response({
                        'error': 'Account already in use',
                        'details': f'The Instagram account @{ig_username} is already linked to another AnyDm user. Please disconnect it from the other account first.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Sync first_name if missing
                if not user.first_name and ig_full_name:
                    user.first_name = ig_full_name
                    user.save()

                if ig_account:
                    # Update without losing the scoped ID
                    final_scoped_id = ig_account.instagram_scoped_id or resolved_scoped_id
                    final_user_id = ig_account.instagram_user_id or resolved_user_id

                    # If we only have resolved_user_id and no scoped_id, we keep final_scoped_id as is (e.g. 27078812251731733)
                    if resolved_scoped_id and resolved_scoped_id != final_user_id:
                        final_scoped_id = resolved_scoped_id

                    ig_account.instagram_scoped_id = final_scoped_id
                    ig_account.instagram_user_id = final_user_id
                    ig_account.user = user
                    ig_account.username = ig_username
                    ig_account.full_name = ig_full_name
                    ig_account.access_token = access_token
                    ig_account.profile_picture_url = ig_profile_pic
                    ig_account.used_for_login = True
                    ig_account.is_active = True
                    ig_account.last_refreshed_at = timezone.now()
                    ig_account.save()
                    created = False
                else:
                    ig_account = InstagramAccount.objects.create(
                        user=user,
                        instagram_scoped_id=resolved_scoped_id,
                        instagram_user_id=resolved_user_id,
                        username=ig_username,
                        full_name=ig_full_name,
                        access_token=access_token,
                        profile_picture_url=ig_profile_pic,
                        used_for_login=True,
                        is_active=True,
                        last_refreshed_at=timezone.now()
                    )
                    created = True
                print(f"[InstagramLogin] Linked account {ig_username} to User(id={user.id}). Created: {created}")
            else:
                # 2. Entry Login Mode (Logged out)
                if ig_account and ig_account.user:
                    # Enforce user-defined login restrictions
                    if not ig_account.used_for_login:
                        return Response({
                            'error': 'Login Restricted',
                            'details': f'Login with @{ig_username} is disabled for this AnyDm account. Please log in with another account or method.'
                        }, status=status.HTTP_403_FORBIDDEN)
                    
                    user = ig_account.user
                    # Update without losing the scoped ID
                    final_scoped_id = ig_account.instagram_scoped_id or resolved_scoped_id
                    final_user_id = ig_account.instagram_user_id or resolved_user_id

                    if resolved_scoped_id and resolved_scoped_id != final_user_id:
                        final_scoped_id = resolved_scoped_id

                    ig_account.instagram_scoped_id = final_scoped_id
                    ig_account.instagram_user_id = final_user_id
                    ig_account.username = ig_username
                    ig_account.access_token = access_token
                    ig_account.profile_picture_url = ig_profile_pic
                    ig_account.is_active = True # Reactivate if it was soft-deleted
                    ig_account.last_refreshed_at = timezone.now()
                    ig_account.save()
                    print(f"[InstagramLogin] Logging in User(id={user.id}) via IG account {ig_username}.")
                else:
                    # Detached or new account: needs a user
                    print(f"[InstagramLogin] Creating/Finding user for IG account {ig_username}.")
                    django_username = f"ig_{ig_username}_{resolved_user_id or resolved_scoped_id}"
                    user, user_created = User.objects.get_or_create(
                        username=django_username,
                        defaults={'first_name': ig_full_name}
                    )
                    
                    if ig_account:
                        ig_account.user = user
                        final_scoped_id = ig_account.instagram_scoped_id or resolved_scoped_id
                        final_user_id = ig_account.instagram_user_id or resolved_user_id

                        if resolved_scoped_id and resolved_scoped_id != final_user_id:
                            final_scoped_id = resolved_scoped_id

                        ig_account.instagram_scoped_id = final_scoped_id
                        ig_account.instagram_user_id = final_user_id
                        ig_account.username = ig_username
                        ig_account.full_name = ig_full_name
                        ig_account.access_token = access_token
                        ig_account.profile_picture_url = ig_profile_pic
                        ig_account.used_for_login = True
                        ig_account.is_active = True
                        ig_account.save()
                    else:
                        ig_account = InstagramAccount.objects.create(
                            user=user,
                            instagram_scoped_id=resolved_scoped_id,
                            instagram_user_id=resolved_user_id,
                            username=ig_username,
                            full_name=ig_full_name,
                            access_token=access_token,
                            profile_picture_url=ig_profile_pic,
                            used_for_login=True,
                            is_active=True,
                            last_refreshed_at=timezone.now()
                        )
                    print(f"[InstagramLogin] Associated User(id={user.id}) with IG account.")
            
            # Update login methods safely
            stored_methods = user.login_methods if isinstance(user.login_methods, list) else []
            if "instagram" not in stored_methods:
                stored_methods.append("instagram")
                user.login_methods = stored_methods
            
            # Set the Instagram account used for login as the active context
            user.active_instagram_account = ig_account
            
            # Ensure firebase_uid is set for consistent identity
            if not user.firebase_uid:
                user.firebase_uid = str(user.id)
            
            user.last_login = timezone.now()
            user.save()
            
            # Generate JWT tokens
            tokens = get_tokens_for_user(user)
            
            # Generate Firebase custom token using the persistent firebase_uid
            from .firebase_auth import create_custom_token
            firebase_token = create_custom_token(user.firebase_uid)

            return Response({
                'message': 'Instagram action successful',
                'tokens': tokens,
                'firebase_token': firebase_token,
                'user': {
                    'id': user.id,
                    'username': user.username,
                    'display_name': ig_account.full_name or ig_account.username,
                    'handle': ig_account.username,
                    'active_instagram_account_id': user.active_instagram_account_id,
                    'login_methods': user.login_methods
                },
                'instagram_account': {
                    'id': ig_account.id,
                    'username': ig_account.username,
                    'instagram_id': ig_account.instagram_scoped_id or ig_account.instagram_user_id,
                    'instagram_global_id': ig_account.instagram_user_id,
                    'profile_picture_url': ig_account.profile_picture_url,
                    'used_for_login': ig_account.used_for_login,
                    'is_enabled': ig_account.is_enabled
                }
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ToggleInstagramLoginView(APIView):
    def post(self, request):
        if not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        account_id = request.data.get('account_id')
        used_for_login = request.data.get('used_for_login')
        
        if account_id is None or used_for_login is None:
            return Response({'error': 'account_id and used_for_login are required'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            user = request.user
            ig_account = InstagramAccount.objects.get(id=account_id)
            if ig_account.user != user:
                return Response({
                    'error': 'Access denied: Account belongs to a different User.',
                    'ig_account_user_id': ig_account.user.id,
                    'request_user_id': user.id
                }, status=status.HTTP_403_FORBIDDEN)
                
            ig_account.used_for_login = bool(used_for_login)
            ig_account.save()
            return Response({'message': 'Success', 'used_for_login': ig_account.used_for_login})
        except InstagramAccount.DoesNotExist:
            return Response({'error': f'Account ID {account_id} not found entirely.'}, status=status.HTTP_404_NOT_FOUND)

class GetConnectedInstagramAccountsView(APIView):
    def get(self, request):
        if not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        user = request.user
        instagram_accounts = InstagramAccount.objects.filter(user=user, is_active=True)
        accounts_data = [
            {
                'id': acc.id, 
                'username': acc.username, 
                'instagram_id': acc.instagram_scoped_id or acc.instagram_user_id,
                'instagram_global_id': acc.instagram_user_id,
                'profile_picture_url': acc.profile_picture_url,
                'used_for_login': acc.used_for_login,
                'is_enabled': acc.is_enabled
            }
            for acc in instagram_accounts
        ]
        
        return Response({'accounts': accounts_data}, status=status.HTTP_200_OK)
        
class InstagramDeauthorizeView(APIView):
    """
    Called by Facebook when a user deauthorizes the Instagram app.
    """
    def post(self, request):
        signed_request = request.data.get('signed_request')
        if not signed_request:
            return Response({'error': 'No signed_request provided'}, status=status.HTTP_400_BAD_REQUEST)
            
        data = parse_signed_request(signed_request)
        if not data:
            return Response({'error': 'Invalid signed_request'}, status=status.HTTP_400_BAD_REQUEST)
            
        ig_id = data.get('user_id')
        if ig_id:
            # Mark the account as not used for login or delete tokens
            # Try to match by scoped ID first (common in newer apps) then global ID
            InstagramAccount.objects.filter(instagram_scoped_id=ig_id).update(
                access_token="",
                used_for_login=False
            )
            InstagramAccount.objects.filter(instagram_user_id=ig_id).update(
                access_token="",
                used_for_login=False
            )
            print(f"[InstagramDeauthorize] Deauthorized Instagram ID: {ig_id}")
            
        return Response({'status': 'deauthorized'}, status=status.HTTP_200_OK)

class InstagramDataDeletionView(APIView):
    """
    Facebook Data Deletion Request Callback.
    """
    def post(self, request):
        signed_request = request.data.get('signed_request')
        if not signed_request:
            return Response({'error': 'No signed_request provided'}, status=status.HTTP_400_BAD_REQUEST)
            
        data = parse_signed_request(signed_request)
        if not data:
            return Response({'error': 'Invalid signed_request'}, status=status.HTTP_400_BAD_REQUEST)
            
        user_id = data.get('user_id')
        # Return the required Facebook response format
        return Response({
            'url': f'https://{request.get_host()}/api/accounts/auth/instagram/deletion-status/?id={user_id}',
            'confirmation_code': f'del_{user_id}'
        }, status=status.HTTP_200_OK)

class UpdateProfileView(APIView):
    def post(self, request):
        if not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        display_name = request.data.get('display_name')
        if display_name is not None:
            user = request.user
            user.first_name = display_name
            user.save()
            return Response({'message': 'Profile updated successfully', 'display_name': user.first_name})
            
        return Response({'error': 'display_name is required'}, status=status.HTTP_400_BAD_REQUEST)

class RemoveInstagramAccountView(APIView):
    """
    Deletes an Instagram account link for the authenticated user.
    """
    def post(self, request):
        if not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        account_id = request.data.get('account_id')
        if not account_id:
            return Response({'error': 'account_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            user = request.user
            ig_account = InstagramAccount.objects.get(id=account_id, user=user)
            username = ig_account.username
            
            # Check if this is the last login method
            other_methods = [m for m in user.login_methods if m != "instagram"]
            active_ig_accounts = InstagramAccount.objects.filter(user=user, is_active=True)
            active_ig_count = active_ig_accounts.count()
            
            is_last_resort = (len(other_methods) == 0 and active_ig_count == 1)
            
            # 1. Detach the Instagram account first
            ig_account.is_active = False
            ig_account.user = None
            ig_account.access_token = ""
            ig_account.refresh_token = ""
            ig_account.save()
            print(f"[RemoveInstagramAccount] Detached IG: {username}")

            # 2. If no other ways to log in, delete the user profile
            if is_last_resort:
                firebase_uid = user.firebase_uid
                if firebase_uid:
                    delete_firebase_user(firebase_uid)
                
                print(f"[RemoveInstagramAccount] Deleting User(id={user.id}) as no login methods remain.")
                user.delete()
                return Response({'message': 'Profile and data removed successfully', 'user_deleted': True}, status=status.HTTP_200_OK)
            
            return Response({'message': 'Account removed successfully', 'user_deleted': False}, status=status.HTTP_200_OK)
        except InstagramAccount.DoesNotExist:
            return Response({'error': 'Account not found or access denied.'}, status=status.HTTP_404_NOT_FOUND)

class ToggleInstagramEnabledView(APIView):
    """
    Toggles the is_enabled status (Actions/Webhooks) for an Instagram account.
    """
    def post(self, request):
        if not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        account_id = request.data.get('account_id')
        is_enabled = request.data.get('is_enabled')
        
        if account_id is None or is_enabled is None:
            return Response({'error': 'account_id and is_enabled are required'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            ig_account = InstagramAccount.objects.get(id=account_id, user=request.user)
            ig_account.is_enabled = bool(is_enabled)
            ig_account.save()
            return Response({'message': 'Status updated', 'is_enabled': ig_account.is_enabled}, status=status.HTTP_200_OK)
        except InstagramAccount.DoesNotExist:
            return Response({'error': 'Account not found or access denied.'}, status=status.HTTP_404_NOT_FOUND)

class SetActiveInstagramAccountView(APIView):
    def post(self, request):
        if not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
            
        account_id = request.data.get('account_id')
        if account_id is None:
            return Response({'error': 'account_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            user = request.user
            ig_account = InstagramAccount.objects.get(id=account_id, user=user, is_active=True)
            user.active_instagram_account = ig_account
            user.save()
            return Response({
                'message': 'Active account updated',
                'active_instagram_account_id': user.active_instagram_account_id
            }, status=status.HTTP_200_OK)
        except InstagramAccount.DoesNotExist:
            return Response({'error': 'Account not found or access denied.'}, status=status.HTTP_404_NOT_FOUND)

class InstagramStoriesView(APIView):
    def get(self, request):
        if not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        
        user = request.user
        active_account = user.active_instagram_account
        if not active_account:
            return Response({'error': 'No active Instagram account connected'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not active_account.access_token:
            return Response({'error': 'Instagram account access token is missing'}, status=status.HTTP_400_BAD_REQUEST)
        
        user_id = active_account.instagram_user_id or active_account.instagram_scoped_id
        if not user_id:
            return Response({'error': 'Instagram user ID is missing'}, status=status.HTTP_400_BAD_REQUEST)
        
        print(f"[InstagramStoriesView] PRE-CALL CREDENTIALS - user_id: {user_id}, access_token: {active_account.access_token}")
        
        is_basic = active_account.access_token.startswith("IGAA")
        host = "graph.instagram.com" if is_basic else "graph.facebook.com"
        url = f"https://{host}/v25.0/{user_id}/stories"
        
        fields = "id,media_type,media_url,permalink,caption,username,timestamp,thumbnail_url"
            
        params = {
            "fields": fields,
            "access_token": active_account.access_token
        }
        
        after_cursor = request.query_params.get("after")
        if after_cursor:
            params["after"] = after_cursor
        
        try:
            r = requests.get(url, params=params)
            r.raise_for_status()
            return Response(r.json(), status=status.HTTP_200_OK)
        except requests.exceptions.RequestException as e:
            print(f"Error fetching Instagram stories: {e}")
            try:
                err_data = r.json()
            except Exception:
                err_data = str(e)
            return Response({'error': 'Failed to fetch stories from Instagram', 'details': err_data}, status=status.HTTP_502_BAD_GATEWAY)

class InstagramMediaListView(APIView):
    def get(self, request):
        if not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        
        user = request.user
        active_account = user.active_instagram_account
        if not active_account:
            return Response({'error': 'No active Instagram account connected'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not active_account.access_token:
            return Response({'error': 'Instagram account access token is missing'}, status=status.HTTP_400_BAD_REQUEST)
        
        user_id = active_account.instagram_user_id or active_account.instagram_scoped_id
        if not user_id:
            return Response({'error': 'Instagram user ID is missing'}, status=status.HTTP_400_BAD_REQUEST)
        
        print(f"[InstagramMediaListView] PRE-CALL CREDENTIALS - user_id: {user_id}, access_token: {active_account.access_token}")
        
        is_basic = active_account.access_token.startswith("IGAA")
        host = "graph.instagram.com" if is_basic else "graph.facebook.com"
        url = f"https://{host}/v25.0/{user_id}/media"
        
        fields = "id,caption,media_type,media_url,permalink,timestamp,like_count,thumbnail_url,children{id,media_type,media_url,permalink,thumbnail_url}"
            
        params = {
            "fields": fields,
            "access_token": active_account.access_token
        }
        
        after_cursor = request.query_params.get("after")
        if after_cursor:
            params["after"] = after_cursor
        
        try:
            r = requests.get(url, params=params)
            r.raise_for_status()
            return Response(r.json(), status=status.HTTP_200_OK)
        except requests.exceptions.RequestException as e:
            print(f"Error fetching Instagram media: {e}")
            try:
                err_data = r.json()
            except Exception:
                err_data = str(e)
            return Response({'error': 'Failed to fetch media from Instagram', 'details': err_data}, status=status.HTTP_502_BAD_GATEWAY)


class InstagramMediaProxyView(APIView):
    def get(self, request):
        from django.http import HttpResponse
        url = request.GET.get('url')
        if not url:
            return Response({'error': 'url parameter is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # For security, restrict proxying to known Instagram and Facebook CDN domains
            if not any(domain in url for domain in ['instagram.com', 'cdninstagram.com', 'facebook.com', 'fbcdn.net']):
                return Response({'error': 'Invalid domain'}, status=status.HTTP_400_BAD_REQUEST)
                
            r = requests.get(url, stream=True, timeout=20)
            r.raise_for_status()
            
            content_type = r.headers.get('content-type', 'image/jpeg')
            response = HttpResponse(r.content, content_type=content_type)
            response["Access-Control-Allow-Origin"] = "*"
            return response
        except Exception as e:
            print(f"[InstagramMediaProxyView] Error proxying media URL {url}: {e}")
            return Response({'error': f'Failed to proxy media: {str(e)}'}, status=status.HTTP_502_BAD_GATEWAY)


class WebsiteSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        active_account = user.active_instagram_account
        if not active_account:
            # Fallback to first active Instagram account if context is missing
            active_account = user.instagram_accounts.filter(is_active=True).first()
            if not active_account:
                return Response({'error': 'No active Instagram account connected.'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Get or create website settings for this active account
        settings_obj, created = WebsiteSettings.objects.get_or_create(
            instagram_account=active_account,
            defaults={
                'store_name': active_account.full_name or active_account.username,
                'store_logo': active_account.profile_picture_url or '',
            }
        )

        return Response({
            'store_name': settings_obj.store_name,
            'store_logo': settings_obj.store_logo,
            'show_related_products': settings_obj.show_related_products,
            'enable_instagram_button': settings_obj.enable_instagram_button,
            'enable_whatsapp_button': settings_obj.enable_whatsapp_button,
            'template_id': settings_obj.template_id,
            'theme_id': settings_obj.theme_id,
            'custom_colors': settings_obj.custom_colors,
            'custom_fonts': settings_obj.custom_fonts,
            'custom_settings': settings_obj.custom_settings,
        }, status=status.HTTP_200_OK)

    def put(self, request):
        user = request.user
        active_account = user.active_instagram_account
        if not active_account:
            active_account = user.instagram_accounts.filter(is_active=True).first()
            if not active_account:
                return Response({'error': 'No active Instagram account connected.'}, status=status.HTTP_400_BAD_REQUEST)

        settings_obj, created = WebsiteSettings.objects.get_or_create(
            instagram_account=active_account,
            defaults={
                'store_name': active_account.full_name or active_account.username,
                'store_logo': active_account.profile_picture_url or '',
            }
        )

        # Update settings fields
        settings_obj.store_name = request.data.get('store_name', settings_obj.store_name)
        
        old_logo = settings_obj.store_logo
        new_logo = request.data.get('store_logo', old_logo)
        if old_logo and new_logo != old_logo:
            # If the old logo was a Cloudinary URL, delete it
            if "res.cloudinary.com" in old_logo:
                try:
                    parts = old_logo.split("/upload/")
                    if len(parts) > 1:
                        path_after_upload = parts[1]
                        if path_after_upload.startswith("v"):
                            path_after_upload = "/".join(path_after_upload.split("/")[1:])
                        public_id = path_after_upload.rsplit(".", 1)[0]
                        
                        from apps.products.models import delete_from_cloudinary
                        delete_from_cloudinary(public_id, "image")
                except Exception as e:
                    import logging
                    local_logger = logging.getLogger(__name__)
                    local_logger.error("Error deleting old logo from Cloudinary: %s", e)

        settings_obj.store_logo = new_logo
        settings_obj.show_related_products = request.data.get('show_related_products', settings_obj.show_related_products)
        settings_obj.enable_instagram_button = request.data.get('enable_instagram_button', settings_obj.enable_instagram_button)
        settings_obj.enable_whatsapp_button = request.data.get('enable_whatsapp_button', settings_obj.enable_whatsapp_button)
        settings_obj.template_id = request.data.get('template_id', settings_obj.template_id)
        settings_obj.theme_id = request.data.get('theme_id', settings_obj.theme_id)
        settings_obj.custom_colors = request.data.get('custom_colors', settings_obj.custom_colors)
        settings_obj.custom_fonts = request.data.get('custom_fonts', settings_obj.custom_fonts)
        settings_obj.custom_settings = request.data.get('custom_settings', settings_obj.custom_settings)
        settings_obj.save()

        return Response({'message': 'Website settings updated successfully'}, status=status.HTTP_200_OK)


class PublicStorefrontView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, username):
        try:
            # Find the active Instagram account by username
            account = InstagramAccount.objects.get(username__iexact=username, is_active=True)
        except InstagramAccount.DoesNotExist:
            return Response({'error': 'Supplier not found'}, status=status.HTTP_404_NOT_FOUND)

        # Get or create website settings
        settings_obj, _ = WebsiteSettings.objects.get_or_create(
            instagram_account=account,
            defaults={
                'store_name': account.full_name or account.username,
                'store_logo': account.profile_picture_url or '',
            }
        )

        # Get active products for this supplier
        products = Product.objects.filter(instagram_account=account, status='ACTIVE').order_by('-created_at')
        products_data = []
        for p in products:
            products_data.append({
                'id': p.id,
                'title': p.title or 'Untitled Product',
                'description': p.description or '',
                'price': str(p.price) if p.price else None,
                'original_price': str(p.original_price) if p.original_price else None,
                'currency': p.currency,
                'main_media_url': p.main_media_url,
                'instagram_permalink': p.instagram_permalink,
                'stock': p.stock,
                'is_negotiable': p.is_negotiable,
            })

        return Response({
            'supplier': {
                'username': account.username,
                'full_name': account.full_name,
                'profile_picture_url': account.profile_picture_url,
            },
            'settings': {
                'store_name': settings_obj.store_name,
                'store_logo': settings_obj.store_logo,
                'show_related_products': settings_obj.show_related_products,
                'enable_instagram_button': settings_obj.enable_instagram_button,
                'enable_whatsapp_button': settings_obj.enable_whatsapp_button,
                'template_id': settings_obj.template_id,
                'theme_id': settings_obj.theme_id,
                'custom_colors': settings_obj.custom_colors,
                'custom_fonts': settings_obj.custom_fonts,
                'custom_settings': settings_obj.custom_settings,
            },
            'products': products_data
        }, status=status.HTTP_200_OK)


class PublicProductDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, username, product_id):
        try:
            account = InstagramAccount.objects.get(username__iexact=username, is_active=True)
        except InstagramAccount.DoesNotExist:
            return Response({'error': 'Supplier not found'}, status=status.HTTP_404_NOT_FOUND)

        try:
            product = Product.objects.get(id=product_id, instagram_account=account, status='ACTIVE')
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        settings_obj, _ = WebsiteSettings.objects.get_or_create(
            instagram_account=account,
            defaults={
                'store_name': account.full_name or account.username,
                'store_logo': account.profile_picture_url or '',
            }
        )

        # Fetch gallery media items
        gallery_data = []
        for g in product.gallery.all().order_by('order'):
            gallery_data.append({
                'id': g.id,
                'media_url': g.media_url,
                'thumbnail_url': g.thumbnail_url,
                'media_type': g.media_type,
                'order': g.order,
            })

        # Fetch related products if enabled
        related_data = []
        if settings_obj.show_related_products:
            related_products = Product.objects.filter(
                instagram_account=account, 
                status='ACTIVE'
            ).exclude(id=product.id).order_by('-created_at')[:4]
            
            for p in related_products:
                related_data.append({
                    'id': p.id,
                    'title': p.title or 'Untitled Product',
                    'price': str(p.price) if p.price else None,
                    'currency': p.currency,
                    'main_media_url': p.main_media_url,
                })

        # Parse metadata
        product_metadata = product.metadata if isinstance(product.metadata, dict) else {}
        variants_string = product_metadata.get('variants', '')
        variants = [v.strip() for v in variants_string.split(',') if v.strip()] if variants_string else []

        return Response({
            'product': {
                'id': product.id,
                'title': product.title or 'Untitled Product',
                'description': product.description or '',
                'price': str(product.price) if product.price else None,
                'original_price': str(product.original_price) if product.original_price else None,
                'currency': product.currency,
                'main_media_url': product.main_media_url,
                'instagram_permalink': product.instagram_permalink,
                'stock': product.stock,
                'is_negotiable': product.is_negotiable,
                'gallery': gallery_data,
                'variants': variants,
                'category': product.category.name if product.category else None,
            },
            'supplier': {
                'username': account.username,
                'full_name': account.full_name,
                'profile_picture_url': account.profile_picture_url,
            },
            'settings': {
                'store_name': settings_obj.store_name,
                'store_logo': settings_obj.store_logo,
                'show_related_products': settings_obj.show_related_products,
                'enable_instagram_button': settings_obj.enable_instagram_button,
                'enable_whatsapp_button': settings_obj.enable_whatsapp_button,
                'template_id': settings_obj.template_id,
                'theme_id': settings_obj.theme_id,
                'custom_colors': settings_obj.custom_colors,
                'custom_fonts': settings_obj.custom_fonts,
                'custom_settings': settings_obj.custom_settings,
            },
            'related_products': related_data
        }, status=status.HTTP_200_OK)



