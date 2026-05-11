import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from .firebase_auth import verify_firebase_token
from .models import InstagramAccount
from django.contrib.auth import get_user_model
from django.conf import settings
User = get_user_model()

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
                    } for acc in instagram_accounts
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

        try:
            # Verify with Instagram
            response = requests.get(
                "https://graph.instagram.com/me",
                params={
                    'fields': 'id,username,name,account_type,profile_picture_url',
                    'access_token': access_token
                }
            )
            
            if response.status_code != 200:
                return Response({'error': 'Invalid Instagram token', 'details': response.json()}, status=status.HTTP_401_UNAUTHORIZED)
            
            data = response.json()
            ig_id = data.get('id')
            ig_username = data.get('username')
            ig_full_name = data.get('name')
            ig_profile_pic = data.get('profile_picture_url')
            
            auth_header = request.headers.get('Authorization', 'No Header')
            print(f"[InstagramLogin] Auth Header: {auth_header}")
            print(f"[InstagramLogin] request.user.is_authenticated: {request.user.is_authenticated}")
            
            # Use update_or_create to strictly enforce uniqueness of instagram_user_id
            # and update the existing record if found.
            if request.user.is_authenticated:
                # 1. Linking Mode (Logged in)
                user = request.user
                print(f"[InstagramLogin] Authenticated Link: User(id={user.id}, email={user.email})")

                # Check if this account already belongs to someone else
                existing_ig = InstagramAccount.objects.filter(instagram_user_id=ig_id).first()
                if existing_ig and existing_ig.user != user:
                    return Response({
                        'error': 'Account already added', 
                        'details': f'The Instagram account @{ig_username} is already linked to another AnyDm user.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Sync first_name if missing
                if not user.first_name and ig_full_name:
                    user.first_name = ig_full_name
                    user.save()
                
                ig_account, created = InstagramAccount.objects.update_or_create(
                    instagram_user_id=ig_id,
                    defaults={
                        'user': user,
                        'username': ig_username,
                        'full_name': ig_full_name,
                        'access_token': access_token,
                        'profile_picture_url': ig_profile_pic,
                        'used_for_login': True
                    }
                )
                print(f"[InstagramLogin] Linked account {ig_username} to User(id={user.id}). Created: {created}")
            else:
                # 2. Entry Login Mode (Logged out)
                # Check if this account already exists
                ig_account = InstagramAccount.objects.filter(instagram_user_id=ig_id).first()
                
                if ig_account:
                    user = ig_account.user
                    # Update info
                    ig_account.username = ig_username
                    ig_account.access_token = access_token
                    ig_account.profile_picture_url = ig_profile_pic
                    ig_account.used_for_login = True
                    ig_account.save()
                    print(f"[InstagramLogin] Logging in existing User(id={user.id}) via IG account {ig_username}")
                else:
                    # Create new user for this new IG account
                    print(f"[InstagramLogin] New IG account {ig_username}. Creating new user.")
                    django_username = f"ig_{ig_username}_{ig_id}"
                    user, user_created = User.objects.get_or_create(
                        username=django_username,
                        defaults={'first_name': ig_full_name}
                    )
                    
                    ig_account = InstagramAccount.objects.create(
                        user=user,
                        instagram_user_id=ig_id,
                        username=ig_username,
                        full_name=ig_full_name,
                        access_token=access_token,
                        profile_picture_url=ig_profile_pic,
                        used_for_login=True
                    )
                    print(f"[InstagramLogin] Created new User(id={user.id}) for IG account. Created: {user_created}")
            
            # Update login methods safely
            stored_methods = user.login_methods if isinstance(user.login_methods, list) else []
            if "instagram" not in stored_methods:
                stored_methods.append("instagram")
                user.login_methods = stored_methods
            
            # Auto-set active account if none selected
            if not user.active_instagram_account:
                user.active_instagram_account = ig_account
            
            # Ensure firebase_uid is set for consistent identity
            if not user.firebase_uid:
                user.firebase_uid = str(user.id)
            
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
                    'instagram_id': ig_account.instagram_user_id,
                    'profile_picture_url': ig_account.profile_picture_url,
                    'used_for_login': ig_account.used_for_login
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
        instagram_accounts = InstagramAccount.objects.filter(user=user)
        accounts_data = [
            {
                'id': acc.id, 
                'username': acc.username, 
                'instagram_id': acc.instagram_user_id,
                'profile_picture_url': acc.profile_picture_url,
                'used_for_login': acc.used_for_login
            }
            for acc in instagram_accounts
        ]
        
        return Response({'accounts': accounts_data}, status=status.HTTP_200_OK)
        
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
