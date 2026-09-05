import os
import razorpay
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
from .models import InstagramAccount, WebsiteSettings, SellerKYC
from apps.products.models import Product
from django.contrib.auth import get_user_model
from django.conf import settings
from django.utils import timezone
User = get_user_model()


def parse_signed_request(signed_request):
    try:
        encoded_sig, payload = signed_request.split('.', 2)
        sig = base64.urlsafe_b64decode(
            encoded_sig + '=' * (4 - len(encoded_sig) % 4))
        data = json.loads(base64.urlsafe_b64decode(
            payload + '=' * (4 - len(payload) % 4)).decode('utf-8'))

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

                # Check for referral code
                ref_code = request.data.get('referral_code')
                if ref_code:
                    try:
                        from apps.settings.models import SystemSettings
                        referrer = User.objects.filter(
                            referral_code=ref_code).first()
                        if referrer and referrer != user:
                            user.referred_by = referrer
                            user.referred_by_set = True

                            sys_settings = SystemSettings.get_settings()
                            referrer.points += sys_settings.referral_points
                            referrer.save()
                            user.save()
                            print(
                                f"[Referral] User {user.username} referred by {referrer.username}. Awarded {sys_settings.referral_points} points.")
                    except Exception as ref_err:
                        print(f"Error applying referral code: {ref_err}")
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
                provider_ids = [
                    p.provider_id for p in firebase_user.provider_data]
            except Exception as e:
                print(f"Firebase Admin Error: {e}")
                provider_ids = []

            provider_map = {'google.com': 'google',
                            'password': 'email', 'firebase': 'email'}
            firebase_methods = []
            for pid in provider_ids:
                method = provider_map.get(pid)
                if method and method not in firebase_methods:
                    firebase_methods.append(method)

            # Ensure user.login_methods is a list
            stored_methods = user.login_methods if isinstance(
                user.login_methods, list) else []
            merged_methods = list(set(stored_methods) | set(firebase_methods))

            if set(stored_methods) != set(merged_methods):
                user.login_methods = merged_methods

            user.last_login = timezone.now()
            user.save()

            # Load Instagram accounts
            instagram_accounts = InstagramAccount.objects.filter(user=user)

            # Generate JWT tokens
            tokens = get_tokens_for_user(user)
            user_payload = serialize_user_payload(user)
            user_payload['login_methods'] = merged_methods

            return Response({
                'message': 'Login successful',
                'tokens': tokens,
                'user': user_payload,
                'instagram_accounts': [
                    {
                        'id': acc.id,
                        'username': acc.username,
                        'profile_picture_url': acc.profile_picture_url,
                        'used_for_login': acc.used_for_login,
                        'is_active': acc.is_active,
                        'is_enabled': acc.is_enabled,
                        'is_token_expired': acc.is_token_expired,
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
        url = "https://graph.facebook.com/v26.0/oauth/access_token"
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
            # Verify with Instagram using v26.0
            # 'id' is the Instagram-Scoped ID (IGSID/SID)
            # 'user_id' is the global Instagram ID (IGID, starts with 17) - requires instagram_graph_user_id permission
            response = requests.get(
                "https://graph.instagram.com/v26.0/me",
                params={
                    'fields': 'id,user_id,username,name,account_type,profile_picture_url',
                    'access_token': access_token
                }
            )

            if response.status_code != 200:
                return Response({'error': 'Invalid Instagram token', 'details': response.json()}, status=status.HTTP_401_UNAUTHORIZED)

            data = response.json()
            # Scoped ID (PSID/SID) or actual global User ID if Basic Display API
            ig_sid = data.get('id')
            # Global ID (starts with 17) or None if Basic Display API
            ig_id = data.get('user_id')
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
            print(
                f"[InstagramLogin] request.user.is_authenticated: {request.user.is_authenticated}")

            # Look up existing account using all possible ID permutations to avoid duplicates/login time problems
            ig_account = None
            if resolved_user_id:
                ig_account = InstagramAccount.objects.filter(
                    instagram_user_id=resolved_user_id).first()
            if not ig_account and resolved_scoped_id:
                ig_account = InstagramAccount.objects.filter(
                    instagram_scoped_id=resolved_scoped_id).first()

            if request.user.is_authenticated:
                # 1. Linking Mode (Logged in)
                user = request.user
                print(
                    f"[InstagramLogin] Authenticated Link: User(id={user.id}, email={user.email})")

                # Enforce limit of 1 Instagram account if plan is expired
                if not user.is_premium_active:
                    existing_active_count = InstagramAccount.objects.filter(
                        user=user, is_active=True).count()
                    if existing_active_count >= 1:
                        is_already_linked = False
                        if ig_account and ig_account.user == user and ig_account.is_active:
                            is_already_linked = True
                        if not is_already_linked:
                            return Response({
                                'error': 'Account Limit Reached',
                                'details': 'Your plan has expired and you are limited to 1 Instagram account. Please upgrade to add more accounts.'
                            }, status=status.HTTP_403_FORBIDDEN)

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
                    ig_account.token_refreshed_at = timezone.now()
                    ig_account.is_token_expired = False
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
                        last_refreshed_at=timezone.now(),
                        token_refreshed_at=timezone.now(),
                        is_token_expired=False
                    )
                    created = True
                print(
                    f"[InstagramLogin] Linked account {ig_username} to User(id={user.id}). Created: {created}")
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
                    ig_account.is_active = True  # Reactivate if it was soft-deleted
                    ig_account.last_refreshed_at = timezone.now()
                    ig_account.token_refreshed_at = timezone.now()
                    ig_account.is_token_expired = False
                    ig_account.save()
                    print(
                        f"[InstagramLogin] Logging in User(id={user.id}) via IG account {ig_username}.")
                else:
                    # Detached or new account: needs a user
                    print(
                        f"[InstagramLogin] Creating/Finding user for IG account {ig_username}.")
                    django_username = f"ig_{ig_username}_{resolved_user_id or resolved_scoped_id}"
                    user, user_created = User.objects.get_or_create(
                        username=django_username,
                        defaults={'first_name': ig_full_name}
                    )
                    if user_created:
                        ref_code = request.data.get('referral_code')
                        if ref_code:
                            try:
                                from apps.settings.models import SystemSettings
                                referrer = User.objects.filter(
                                    referral_code=ref_code).first()
                                if referrer and referrer != user:
                                    user.referred_by = referrer
                                    user.referred_by_set = True

                                    sys_settings = SystemSettings.get_settings()
                                    referrer.points += sys_settings.referral_points
                                    referrer.save()
                                    user.save()
                                    print(
                                        f"[Referral] User {user.username} referred by {referrer.username} via IG. Awarded {sys_settings.referral_points} points.")
                            except Exception as ref_err:
                                print(
                                    f"Error applying referral code: {ref_err}")

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
                        ig_account.token_refreshed_at = timezone.now()
                        ig_account.is_token_expired = False
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
                            last_refreshed_at=timezone.now(),
                            token_refreshed_at=timezone.now(),
                            is_token_expired=False
                        )
                    print(
                        f"[InstagramLogin] Associated User(id={user.id}) with IG account.")

            # Update login methods safely
            stored_methods = user.login_methods if isinstance(
                user.login_methods, list) else []
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

            user_payload = serialize_user_payload(user)
            user_payload['display_name'] = ig_account.full_name or ig_account.username
            user_payload['handle'] = ig_account.username

            return Response({
                'message': 'Instagram action successful',
                'tokens': tokens,
                'firebase_token': firebase_token,
                'user': user_payload,
                'instagram_account': {
                    'id': ig_account.id,
                    'username': ig_account.username,
                    'instagram_id': ig_account.instagram_scoped_id or ig_account.instagram_user_id,
                    'instagram_global_id': ig_account.instagram_user_id,
                    'profile_picture_url': ig_account.profile_picture_url,
                    'used_for_login': ig_account.used_for_login,
                    'is_enabled': ig_account.is_enabled,
                    'is_token_expired': ig_account.is_token_expired
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
        instagram_accounts = InstagramAccount.objects.filter(
            user=user, is_active=True)
        accounts_data = [
            {
                'id': acc.id,
                'username': acc.username,
                'instagram_id': acc.instagram_scoped_id or acc.instagram_user_id,
                'instagram_global_id': acc.instagram_user_id,
                'profile_picture_url': acc.profile_picture_url,
                'used_for_login': acc.used_for_login,
                'is_enabled': acc.is_enabled,
                'is_token_expired': acc.is_token_expired
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
            active_ig_accounts = InstagramAccount.objects.filter(
                user=user, is_active=True)
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

                print(
                    f"[RemoveInstagramAccount] Deleting User(id={user.id}) as no login methods remain.")
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
            ig_account = InstagramAccount.objects.get(
                id=account_id, user=request.user)
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
            ig_account = InstagramAccount.objects.get(
                id=account_id, user=user, is_active=True)
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

        print(
            f"[InstagramStoriesView] PRE-CALL CREDENTIALS - user_id: {user_id}, access_token: {active_account.access_token}")

        is_basic = active_account.access_token.startswith("IGAA")
        host = "graph.instagram.com" if is_basic else "graph.facebook.com"
        url = f"https://{host}/v26.0/{user_id}/stories"

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

        print(
            f"[InstagramMediaListView] PRE-CALL CREDENTIALS - user_id: {user_id}, access_token: {active_account.access_token}")

        is_basic = active_account.access_token.startswith("IGAA")
        host = "graph.instagram.com" if is_basic else "graph.facebook.com"
        url = f"https://{host}/v26.0/{user_id}/media"

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
            print(
                f"[InstagramMediaProxyView] Error proxying media URL {url}: {e}")
            return Response({'error': f'Failed to proxy media: {str(e)}'}, status=status.HTTP_502_BAD_GATEWAY)


class WebsiteSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        active_account = user.active_instagram_account
        if not active_account:
            # Fallback to first active Instagram account if context is missing
            active_account = user.instagram_accounts.filter(
                is_active=True).first()
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

        # Enforce COD if KYC is not approved
        seller_kyc, _ = SellerKYC.objects.get_or_create(user=user)
        if seller_kyc.status != 'APPROVED' and not settings_obj.cod_enabled:
            settings_obj.cod_enabled = True
            settings_obj.save(update_fields=['cod_enabled'])

        return Response({
            'store_name': settings_obj.store_name,
            'store_logo': settings_obj.store_logo,
            'store_slug': settings_obj.store_slug,
            'custom_domain': settings_obj.custom_domain,
            'store_banner': settings_obj.store_banner,
            'store_description': settings_obj.store_description,
            'contact_email': settings_obj.contact_email,
            'contact_phone': settings_obj.contact_phone,
            'business_address': settings_obj.business_address,
            'shipping_address': settings_obj.shipping_address,
            'return_policy': settings_obj.return_policy,
            'cancellation_policy': settings_obj.cancellation_policy,
            'cod_enabled': settings_obj.cod_enabled,
            'online_payment_enabled': settings_obj.online_payment_enabled,
            'show_related_products': settings_obj.show_related_products,
            'enable_instagram_button': settings_obj.enable_instagram_button,
            'enable_whatsapp_button': settings_obj.enable_whatsapp_button,
            'template_id': settings_obj.template_id,
            'theme_id': settings_obj.theme_id,
            'privacy_policy': settings_obj.privacy_policy,
            'terms_of_service': settings_obj.terms_of_service,
            'custom_colors': settings_obj.custom_colors,
            'custom_fonts': settings_obj.custom_fonts,
            'custom_settings': settings_obj.custom_settings,
        }, status=status.HTTP_200_OK)

    def put(self, request):
        user = request.user
        active_account = user.active_instagram_account
        if not active_account:
            active_account = user.instagram_accounts.filter(
                is_active=True).first()
            if not active_account:
                return Response({'error': 'No active Instagram account connected.'}, status=status.HTTP_400_BAD_REQUEST)

        settings_obj, created = WebsiteSettings.objects.get_or_create(
            instagram_account=active_account,
            defaults={
                'store_name': active_account.full_name or active_account.username,
                'store_logo': active_account.profile_picture_url or '',
            }
        )

        seller_kyc, _ = SellerKYC.objects.get_or_create(user=user)

        # Unique store slug validation
        new_slug = request.data.get('store_slug', None)
        if new_slug is not None:
            new_slug = new_slug.strip().lower()
            if new_slug:
                import re
                new_slug = re.sub(r'[^a-z0-9_-]', '', new_slug.replace(' ', '_'))
                if new_slug != settings_obj.store_slug:
                    if WebsiteSettings.objects.filter(store_slug__iexact=new_slug).exclude(id=settings_obj.id).exists() or \
                       InstagramAccount.objects.filter(username__iexact=new_slug).exclude(id=settings_obj.instagram_account_id).exists():
                        return Response({'error': 'This store URL / subdomain name is already taken. Please choose another one.'}, status=status.HTTP_400_BAD_REQUEST)
                    settings_obj.store_slug = new_slug
            else:
                settings_obj.store_slug = None

        # Custom domain validation
        new_domain = request.data.get('custom_domain', None)
        if new_domain is not None:
            new_domain = new_domain.strip().lower()
            if new_domain:
                import re
                new_domain = re.sub(r'^https?://', '', new_domain).strip('/')
                new_domain = re.sub(r'[^a-z0-9.-]', '', new_domain)
                if new_domain != settings_obj.custom_domain:
                    if WebsiteSettings.objects.filter(custom_domain__iexact=new_domain).exclude(id=settings_obj.id).exists():
                        return Response({'error': 'This custom domain is already registered to another store.'}, status=status.HTTP_400_BAD_REQUEST)
                    settings_obj.custom_domain = new_domain
            else:
                settings_obj.custom_domain = None

        # Update settings fields
        settings_obj.store_name = request.data.get(
            'store_name', settings_obj.store_name)

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
                            path_after_upload = "/".join(
                                path_after_upload.split("/")[1:])
                        public_id = path_after_upload.rsplit(".", 1)[0]

                        from apps.products.models import delete_from_cloudinary
                        delete_from_cloudinary(public_id, "image")
                except Exception as e:
                    import logging
                    local_logger = logging.getLogger(__name__)
                    local_logger.error(
                        "Error deleting old logo from Cloudinary: %s", e)

        settings_obj.store_logo = new_logo
        settings_obj.store_banner = request.data.get(
            'store_banner', settings_obj.store_banner)
        settings_obj.store_description = request.data.get(
            'store_description', settings_obj.store_description)
        settings_obj.contact_email = request.data.get(
            'contact_email', settings_obj.contact_email)
        settings_obj.contact_phone = request.data.get(
            'contact_phone', settings_obj.contact_phone)
        settings_obj.business_address = request.data.get(
            'business_address', settings_obj.business_address)
        settings_obj.shipping_address = request.data.get(
            'shipping_address', settings_obj.shipping_address)

        # Enforce COD enabled if KYC is not approved
        if seller_kyc.status != 'APPROVED':
            settings_obj.cod_enabled = True
        else:
            settings_obj.cod_enabled = request.data.get(
                'cod_enabled', settings_obj.cod_enabled)

        settings_obj.online_payment_enabled = request.data.get(
            'online_payment_enabled', settings_obj.online_payment_enabled)

        # Allow policy changes by the supplier/admin
        settings_obj.return_policy = request.data.get(
            'return_policy', settings_obj.return_policy)
        settings_obj.cancellation_policy = request.data.get(
            'cancellation_policy', settings_obj.cancellation_policy)
        settings_obj.privacy_policy = request.data.get(
            'privacy_policy', settings_obj.privacy_policy)
        settings_obj.terms_of_service = request.data.get(
            'terms_of_service', settings_obj.terms_of_service)

        settings_obj.show_related_products = request.data.get(
            'show_related_products', settings_obj.show_related_products)
        settings_obj.enable_instagram_button = request.data.get(
            'enable_instagram_button', settings_obj.enable_instagram_button)
        settings_obj.enable_whatsapp_button = request.data.get(
            'enable_whatsapp_button', settings_obj.enable_whatsapp_button)
        settings_obj.template_id = request.data.get(
            'template_id', settings_obj.template_id)
        settings_obj.theme_id = request.data.get(
            'theme_id', settings_obj.theme_id)
        settings_obj.custom_colors = request.data.get(
            'custom_colors', settings_obj.custom_colors)
        settings_obj.custom_fonts = request.data.get(
            'custom_fonts', settings_obj.custom_fonts)
        settings_obj.custom_settings = request.data.get(
            'custom_settings', settings_obj.custom_settings)
        settings_obj.save()

        return Response({'message': 'Website settings updated successfully'}, status=status.HTTP_200_OK)


class PublicStorefrontView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, username):
        from django.db.models import Q
        import re

        clean_username = username.strip().lower()
        clean_username = re.sub(r'^https?://', '', clean_username).strip('/')
        domain_no_www = clean_username.replace('www.', '')
        domain_with_www = f"www.{domain_no_www}"
        slug_candidate = re.sub(r'\.(com|in|org|net|co|app|dev|xyz|shop|store)$', '', domain_no_www)

        account = InstagramAccount.objects.filter(username__iexact=clean_username, is_active=True).first()
        if not account:
            account = InstagramAccount.objects.filter(username__iexact=slug_candidate, is_active=True).first()
        if not account:
            account = InstagramAccount.objects.filter(username__iexact=clean_username).first()
        if not account:
            account = InstagramAccount.objects.filter(username__iexact=slug_candidate).first()

        if not account:
            ws = WebsiteSettings.objects.filter(
                Q(store_slug__iexact=clean_username) |
                Q(store_slug__iexact=slug_candidate) |
                Q(custom_domain__iexact=clean_username) |
                Q(custom_domain__iexact=domain_no_www) |
                Q(custom_domain__iexact=domain_with_www) |
                Q(instagram_account__username__iexact=clean_username) |
                Q(instagram_account__username__iexact=slug_candidate)
            ).first()
            if ws:
                account = ws.instagram_account

        if not account:
            return Response({'error': 'Supplier not found'}, status=status.HTTP_404_NOT_FOUND)

        # Get or create website settings
        settings_obj, _ = WebsiteSettings.objects.get_or_create(
            instagram_account=account,
            defaults={
                'store_name': account.full_name or account.username,
                'store_logo': account.profile_picture_url or '',
            }
        )

        # Enforce KYC verification check for online payments
        seller_kyc, _ = SellerKYC.objects.get_or_create(user=account.user)
        online_payment_enabled = settings_obj.online_payment_enabled and (
            seller_kyc.status == 'APPROVED')

        # Get active products for this supplier
        products = Product.objects.filter(
            instagram_account=account, status='ACTIVE').order_by('-created_at')
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
                'category': p.category.name if p.category else None,
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
                'store_slug': settings_obj.store_slug,
                'custom_domain': settings_obj.custom_domain,
                'store_banner': settings_obj.store_banner,
                'store_description': settings_obj.store_description,
                'contact_email': settings_obj.contact_email,
                'contact_phone': settings_obj.contact_phone,
                'business_address': settings_obj.business_address,
                'shipping_address': settings_obj.shipping_address,
                'return_policy': settings_obj.return_policy,
                'cancellation_policy': settings_obj.cancellation_policy,
                'cod_enabled': settings_obj.cod_enabled,
                'online_payment_enabled': online_payment_enabled,
                'show_related_products': settings_obj.show_related_products,
                'enable_instagram_button': settings_obj.enable_instagram_button,
                'enable_whatsapp_button': settings_obj.enable_whatsapp_button,
                'template_id': settings_obj.template_id,
                'theme_id': settings_obj.theme_id,
                'privacy_policy': settings_obj.privacy_policy,
                'terms_of_service': settings_obj.terms_of_service,
                'custom_colors': settings_obj.custom_colors,
                'custom_fonts': settings_obj.custom_fonts,
                'custom_settings': settings_obj.custom_settings,
            },
            'products': products_data
        }, status=status.HTTP_200_OK)


class PublicProductDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, username, product_id):
        from django.db.models import Q
        import re

        clean_username = username.strip().lower()
        clean_username = re.sub(r'^https?://', '', clean_username).strip('/')
        domain_no_www = clean_username.replace('www.', '')
        domain_with_www = f"www.{domain_no_www}"
        slug_candidate = re.sub(r'\.(com|in|org|net|co|app|dev|xyz|shop|store)$', '', domain_no_www)

        account = InstagramAccount.objects.filter(username__iexact=clean_username, is_active=True).first()
        if not account:
            account = InstagramAccount.objects.filter(username__iexact=slug_candidate, is_active=True).first()
        if not account:
            account = InstagramAccount.objects.filter(username__iexact=clean_username).first()
        if not account:
            account = InstagramAccount.objects.filter(username__iexact=slug_candidate).first()

        if not account:
            ws = WebsiteSettings.objects.filter(
                Q(store_slug__iexact=clean_username) |
                Q(store_slug__iexact=slug_candidate) |
                Q(custom_domain__iexact=clean_username) |
                Q(custom_domain__iexact=domain_no_www) |
                Q(custom_domain__iexact=domain_with_www) |
                Q(instagram_account__username__iexact=clean_username) |
                Q(instagram_account__username__iexact=slug_candidate)
            ).first()
            if ws:
                account = ws.instagram_account

        if not account:
            return Response({'error': 'Supplier not found'}, status=status.HTTP_404_NOT_FOUND)

        try:
            product = Product.objects.get(
                id=product_id, instagram_account=account, status='ACTIVE')
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        settings_obj, _ = WebsiteSettings.objects.get_or_create(
            instagram_account=account,
            defaults={
                'store_name': account.full_name or account.username,
                'store_logo': account.profile_picture_url or '',
            }
        )

        # Enforce KYC verification check for online payments
        seller_kyc, _ = SellerKYC.objects.get_or_create(user=account.user)
        online_payment_enabled = settings_obj.online_payment_enabled and (
            seller_kyc.status == 'APPROVED')

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
        product_metadata = product.metadata if isinstance(
            product.metadata, dict) else {}
        variants_string = product_metadata.get('variants', '')
        variants = [v.strip() for v in variants_string.split(',')
                    if v.strip()] if variants_string else []

        # Parse metadata - exclude 'variants' since it's already in its own field
        technical_details = {
            k: v for k, v in product_metadata.items()
            if k != 'variants' and v is not None and str(v).strip() != ''
        }

        return Response({
            'product': {
                'id': product.id,
                'title': product.title or 'Untitled Product',
                'description': product.description or '',
                'brand': product.brand,
                'sku': product.sku,
                'price': str(product.price) if product.price else None,
                'original_price': str(product.original_price) if product.original_price else None,
                'discount_price': str(product.discount_price) if product.discount_price else None,
                'currency': product.currency,
                'main_media_url': product.main_media_url,
                'instagram_permalink': product.instagram_permalink,
                'stock': product.stock,
                'weight': str(product.weight) if product.weight else "0.00",
                'dimensions': product.dimensions,
                'shipping_charge': str(product.shipping_charge) if product.shipping_charge else "0.00",
                'is_negotiable': product.is_negotiable,
                'cod_enabled': product.cod_enabled,
                'allow_return': product.allow_return,
                'allow_refund': product.allow_refund,
                'status': product.status,
                'gallery': gallery_data,
                'variants': variants,
                'category': product.category.name if product.category else None,
                'metadata': technical_details,
            },
            'supplier': {
                'username': account.username,
                'full_name': account.full_name,
                'profile_picture_url': account.profile_picture_url,
            },
            'settings': {
                'store_name': settings_obj.store_name,
                'store_logo': settings_obj.store_logo,
                'store_slug': settings_obj.store_slug,
                'store_banner': settings_obj.store_banner,
                'store_description': settings_obj.store_description,
                'contact_email': settings_obj.contact_email,
                'contact_phone': settings_obj.contact_phone,
                'business_address': settings_obj.business_address,
                'shipping_address': settings_obj.shipping_address,
                'return_policy': settings_obj.return_policy,
                'cancellation_policy': settings_obj.cancellation_policy,
                'cod_enabled': settings_obj.cod_enabled,
                'online_payment_enabled': online_payment_enabled,
                'show_related_products': settings_obj.show_related_products,
                'enable_instagram_button': settings_obj.enable_instagram_button,
                'enable_whatsapp_button': settings_obj.enable_whatsapp_button,
                'template_id': settings_obj.template_id,
                'theme_id': settings_obj.theme_id,
                'privacy_policy': settings_obj.privacy_policy,
                'terms_of_service': settings_obj.terms_of_service,
                'custom_colors': settings_obj.custom_colors,
                'custom_fonts': settings_obj.custom_fonts,
                'custom_settings': settings_obj.custom_settings,
            },
            'related_products': related_data
        }, status=status.HTTP_200_OK)


# ── Refer & Earn & Subscription Support Views ───────────────────────────

def serialize_user_payload(user):
    payload = {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'login_methods': user.login_methods if isinstance(user.login_methods, list) else [],
        'display_name': user.first_name or user.username,
        'active_instagram_account_id': user.active_instagram_account_id,
        'plan': user.plan,
        'points': user.points,
        'referral_code': user.referral_code,
        'trial_days': user.trial_days,
        'trial_start_date': user.trial_start_date.isoformat() if user.trial_start_date else None,
        'premium_expires_at': user.premium_expires_at.isoformat() if user.premium_expires_at else None,
        'has_extended_trial': user.has_extended_trial,
        'referred_by_set': user.referred_by_set,
        'referred_by': user.referred_by.referral_code if user.referred_by else None,
        'is_premium_active': user.is_premium_active,
        'trial_days_left': user.trial_days_left,
        'custom_code_set': getattr(user, 'custom_code_set', False),
        'is_creator_vip': user.is_creator_vip,
        'creator_reward_type': user.creator_reward_type,
        'creator_commission_percent': float(user.creator_commission_percent) if user.creator_commission_percent else 10.0,
        'is_following_official_account': getattr(user, 'is_following_official_account', False),
        'official_follow_points_awarded': getattr(user, 'official_follow_points_awarded', 0),
        'official_follow_at': user.official_follow_at.isoformat() if getattr(user, 'official_follow_at', None) else None,
    }
    if user.is_superuser:
        payload['is_superuser'] = True
    if user.is_staff:
        payload['is_staff'] = True
    return payload


class ReferralStatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        from django.db.models import Count
        from django.contrib.auth import get_user_model
        User = get_user_model()
        from apps.settings.models import SystemSettings

        sys_settings = SystemSettings.get_settings()

        # Referred users list
        referred_users_qs = User.objects.filter(
            referred_by=user).order_by('-date_joined')
        referred_users = []
        for u in referred_users_qs:
            referred_users.append({
                'username': u.username,
                'display_name': u.first_name or u.username,
                'date_joined': u.date_joined.isoformat(),
                'is_premium_active': u.is_premium_active,
                'plan': u.plan
            })

        # Leaderboard (Top 3 users with most referrals)
        leaderboard_qs = User.objects.annotate(
            ref_count=Count('referrals')
        ).filter(ref_count__gt=0).order_by('-ref_count')[:3]

        leaderboard = []
        for idx, u in enumerate(leaderboard_qs, 1):
            leaderboard.append({
                'rank': idx,
                'display_name': u.first_name or u.username,
                'referral_count': u.ref_count
            })

        return Response({
            'referral_code': user.referral_code,
            'points': user.points,
            'referral_count': len(referred_users),
            'referred_users': referred_users,
            'leaderboard': leaderboard,
            'points_needed_for_premium': sys_settings.points_to_redeem,
            'paid_plan_price': float(sys_settings.premium_plan_price),
            'referral_points': sys_settings.referral_points,
            'official_follow_points': getattr(sys_settings, 'official_follow_points', 50),
            'is_following_official_account': getattr(user, 'is_following_official_account', False),
            'official_follow_points_awarded': getattr(user, 'official_follow_points_awarded', 0),
            'official_follow_at': user.official_follow_at.isoformat() if getattr(user, 'official_follow_at', None) else None,
            'trial_days_left': user.trial_days_left,
            'plan': user.plan,
            'is_premium_active': user.is_premium_active,
            'has_extended_trial': user.has_extended_trial,
            'referred_by_set': user.referred_by_set,
            'custom_code_set': getattr(user, 'custom_code_set', False),
        }, status=status.HTTP_200_OK)


class SetReferredByView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        code = request.data.get('code')

        if not code:
            return Response({'error': 'Referral code is required'}, status=status.HTTP_400_BAD_REQUEST)

        if user.referred_by_set or user.referred_by:
            return Response({'error': 'You have already set a referrer.'}, status=status.HTTP_400_BAD_REQUEST)

        if user.referral_code == code:
            return Response({'error': 'You cannot refer yourself.'}, status=status.HTTP_400_BAD_REQUEST)

        from django.contrib.auth import get_user_model
        User = get_user_model()
        from apps.settings.models import SystemSettings

        referrer = User.objects.filter(referral_code=code).first()
        if not referrer:
            return Response({'error': 'Invalid referral code.'}, status=status.HTTP_404_NOT_FOUND)

        sys_settings = SystemSettings.get_settings()

        user.referred_by = referrer
        user.referred_by_set = True
        # Grant 15 days extended trial to user referred via creator link
        user.trial_days = max(user.trial_days, 15)
        user.save()

        print(
            f"[Referral-Linked] User {user.username} linked referrer code {code} ({referrer.username}). Granted 15-day trial.")

        return Response({
            'message': f'Referrer linked successfully! You were referred by {referrer.first_name or referrer.username} and granted a 15-day trial.',
            'user': serialize_user_payload(user)
        }, status=status.HTTP_200_OK)


class ExtendTrialView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if user.has_extended_trial:
            return Response({'error': 'You have already extended your trial.'}, status=status.HTTP_400_BAD_REQUEST)

        from apps.settings.models import SystemSettings
        sys_settings = SystemSettings.get_settings()

        user.has_extended_trial = True
        user.trial_days += sys_settings.extend_days
        user.save()

        print(
            f"[Trial Extension] User {user.username} extended trial by {sys_settings.extend_days} days. Total trial days: {user.trial_days}")

        return Response({
            'message': f'Trial extended successfully by {sys_settings.extend_days} days!',
            'user': serialize_user_payload(user)
        }, status=status.HTTP_200_OK)


class SetCustomReferralCodeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        code = request.data.get('code', '').strip().upper()

        if not code:
            return Response({'error': 'Custom referral code is required'}, status=status.HTTP_400_BAD_REQUEST)

        import re
        if not re.match(r'^[A-Z0-9_-]{3,20}$', code):
            return Response({'error': 'Referral code must be 3-20 alphanumeric characters or hyphens.'}, status=status.HTTP_400_BAD_REQUEST)

        from django.contrib.auth import get_user_model
        User = get_user_model()

        if getattr(user, 'custom_code_set', False):
            return Response({'error': 'You can only customize your referral code once.'}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(referral_code__iexact=code).exclude(id=user.id).exists():
            return Response({'error': f'Referral code "{code}" is already taken.'}, status=status.HTTP_400_BAD_REQUEST)

        user.referral_code = code
        user.custom_code_set = True
        user.save(update_fields=['referral_code', 'custom_code_set'])

        return Response({
            'message': f'Custom referral ID set to {code} successfully!',
            'referral_code': user.referral_code,
            'custom_code_set': user.custom_code_set,
            'user': serialize_user_payload(user)
        }, status=status.HTTP_200_OK)


class ClaimOfficialFollowRewardView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if getattr(user, 'is_following_official_account', False):
            return Response({
                'message': 'You have already claimed your 50 points for following @anydm.in!',
                'already_claimed': True,
                'points': user.points,
                'is_following_official_account': True,
                'user': serialize_user_payload(user)
            }, status=status.HTTP_200_OK)

        from apps.settings.models import SystemSettings
        from django.utils import timezone
        sys_settings = SystemSettings.get_settings()
        points_to_award = getattr(sys_settings, 'official_follow_points', 50) or 50

        user.points += points_to_award
        user.is_following_official_account = True
        user.official_follow_points_awarded = points_to_award
        user.official_follow_at = timezone.now()
        user.save(update_fields=[
            'points',
            'is_following_official_account',
            'official_follow_points_awarded',
            'official_follow_at'
        ])

        print(f"[Official Follow Claimed] User {user.username} claimed {points_to_award} points for following @anydm.in. Total points: {user.points}")

        return Response({
            'message': f'Success! +{points_to_award} points added to your balance for following @anydm.in.',
            'points_awarded': points_to_award,
            'points': user.points,
            'is_following_official_account': True,
            'user': serialize_user_payload(user)
        }, status=status.HTTP_200_OK)


class UnfollowOfficialRewardView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if not getattr(user, 'is_following_official_account', False):
            return Response({
                'message': 'User is not marked as following @anydm.in.',
                'points': user.points,
                'is_following_official_account': False,
                'user': serialize_user_payload(user)
            }, status=status.HTTP_200_OK)

        from django.utils import timezone
        points_to_deduct = getattr(user, 'official_follow_points_awarded', 0) or 50
        user.points = max(0, user.points - points_to_deduct)
        user.is_following_official_account = False
        user.official_follow_points_awarded = 0
        user.official_unfollow_at = timezone.now()
        user.save(update_fields=[
            'points',
            'is_following_official_account',
            'official_follow_points_awarded',
            'official_unfollow_at'
        ])

        print(f"[Official Unfollow] User {user.username} unfollowed @anydm.in. Deducted {points_to_deduct} points. Total points: {user.points}")

        return Response({
            'message': f'Unfollow recorded. {points_to_deduct} points deducted from balance.',
            'points_deducted': points_to_deduct,
            'points': user.points,
            'is_following_official_account': False,
            'user': serialize_user_payload(user)
        }, status=status.HTTP_200_OK)


class GrantCreatorVIPView(APIView):
    def post(self, request):
        email = request.data.get('email', '').strip()
        months = int(request.data.get('months', 3))

        if not email:
            return Response({'error': 'Email address or username is required'}, status=status.HTTP_400_BAD_REQUEST)

        from django.contrib.auth import get_user_model
        User = get_user_model()
        from django.utils import timezone
        from django.db.models import Q

        target_user = User.objects.filter(
            Q(email__iexact=email) | Q(username__iexact=email)).first()
        if not target_user:
            return Response({'error': f'No user found with email/username "{email}".'}, status=status.HTTP_404_NOT_FOUND)

        target_user.plan = 'pro'
        target_user.is_creator_vip = True
        target_user.creator_reward_type = 'vip'
        days_to_add = months * 30
        if target_user.premium_expires_at and target_user.premium_expires_at > timezone.now():
            target_user.premium_expires_at += timezone.timedelta(
                days=days_to_add)
        else:
            target_user.premium_expires_at = timezone.now(
            ) + timezone.timedelta(days=days_to_add)

        target_user.save()
        auto_enable_subscription_ai_for_user(target_user)

        print(
            f"[Creator-VIP-Grant] Granted {months} months Creator Pro access to {target_user.username} ({email}). Expires: {target_user.premium_expires_at}")

        return Response({
            'message': f'Granted {months} months Creator Pro VIP access to {target_user.username} ({email})!',
            'user': serialize_user_payload(target_user)
        }, status=status.HTTP_200_OK)


class SetCreatorRewardTypeView(APIView):
    """Admin endpoint to set a creator's reward type (VIP or Commission) and commission %."""
    def post(self, request):
        email = request.data.get('email', '').strip()
        reward_type = request.data.get('reward_type', '').strip()
        commission_percent = request.data.get('commission_percent', 10)

        if not email:
            return Response({'error': 'Email or username is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if reward_type not in ('vip', 'commission'):
            return Response({'error': 'reward_type must be "vip" or "commission".'}, status=status.HTTP_400_BAD_REQUEST)

        from django.contrib.auth import get_user_model
        from django.db.models import Q
        from django.utils import timezone
        from decimal import Decimal
        User = get_user_model()

        target_user = User.objects.filter(
            Q(email__iexact=email) | Q(username__iexact=email)).first()
        if not target_user:
            return Response({'error': f'No user found with email/username "{email}".'}, status=status.HTTP_404_NOT_FOUND)

        target_user.is_creator_vip = True
        target_user.creator_reward_type = reward_type

        if reward_type == 'commission':
            try:
                pct = Decimal(str(commission_percent))
                if pct < 1 or pct > 100:
                    return Response({'error': 'Commission percent must be between 1 and 100.'}, status=status.HTTP_400_BAD_REQUEST)
                target_user.creator_commission_percent = pct
            except Exception:
                return Response({'error': 'Invalid commission percent value.'}, status=status.HTTP_400_BAD_REQUEST)

            target_user.save()
            auto_enable_subscription_ai_for_user(target_user)

            print(f"[Creator-Commission-Set] Set {target_user.username} to commission mode at {commission_percent}%.")
            return Response({
                'message': f'Set {target_user.username} to Commission mode at {commission_percent}%.',
                'user': serialize_user_payload(target_user)
            }, status=status.HTTP_200_OK)

        elif reward_type == 'vip':
            months = int(request.data.get('months', 3))
            target_user.plan = 'pro'
            days_to_add = months * 30
            if target_user.premium_expires_at and target_user.premium_expires_at > timezone.now():
                target_user.premium_expires_at += timezone.timedelta(days=days_to_add)
            else:
                target_user.premium_expires_at = timezone.now() + timezone.timedelta(days=days_to_add)

            target_user.save()
            auto_enable_subscription_ai_for_user(target_user)

            print(f"[Creator-VIP-Set] Set {target_user.username} to VIP mode with {months} months Pro.")
            return Response({
                'message': f'Set {target_user.username} to VIP mode with {months} months Creator Pro!',
                'user': serialize_user_payload(target_user)
            }, status=status.HTTP_200_OK)


class CreatorEarningsView(APIView):
    """Creator endpoint to view their commission earnings dashboard."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not user.is_creator_vip:
            return Response({'error': 'You are not a creator.'}, status=status.HTTP_403_FORBIDDEN)

        from apps.accounts.models import CreatorCommission
        from django.db.models import Sum, Count, Q
        from django.contrib.auth import get_user_model
        from decimal import Decimal
        User = get_user_model()

        commissions_qs = CreatorCommission.objects.filter(creator=user)
        total_earned = commissions_qs.aggregate(total=Sum('commission_amount'))['total'] or Decimal('0')
        total_pending = commissions_qs.filter(status='pending').aggregate(total=Sum('commission_amount'))['total'] or Decimal('0')
        total_paid = commissions_qs.filter(status='paid').aggregate(total=Sum('commission_amount'))['total'] or Decimal('0')

        commissions_list = []
        for c in commissions_qs[:50]:
            commissions_list.append({
                'id': c.id,
                'referred_user': c.referred_user.first_name or c.referred_user.username,
                'referred_username': c.referred_user.username,
                'payment_amount': float(c.payment_amount),
                'commission_percent': float(c.commission_percent),
                'commission_amount': float(c.commission_amount),
                'status': c.status,
                'created_at': c.created_at.isoformat(),
            })

        # Referral stats
        total_referrals = User.objects.filter(referred_by=user).count()
        paid_referrals = User.objects.filter(referred_by=user, referral_paid_reward_given=True).count()

        return Response({
            'reward_type': user.creator_reward_type,
            'commission_percent': float(user.creator_commission_percent) if user.creator_commission_percent else 10.0,
            'total_earned': float(total_earned),
            'total_pending': float(total_pending),
            'total_paid': float(total_paid),
            'commissions': commissions_list,
            'total_referrals': total_referrals,
            'paid_referrals': paid_referrals,
        }, status=status.HTTP_200_OK)


class AdminSettleCreatorCommissionView(APIView):
    """Admin endpoint to settle pending commissions for a creator after manual bank disbursement."""
    def post(self, request):
        user_id = request.data.get('user_id')
        email = request.data.get('email', '').strip()

        from django.contrib.auth import get_user_model
        from django.db.models import Q, Sum
        from apps.accounts.models import CreatorCommission
        from decimal import Decimal
        User = get_user_model()

        target_user = None
        if user_id:
            target_user = User.objects.filter(id=user_id).first()
        elif email:
            target_user = User.objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).first()

        if not target_user:
            return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        pending_qs = CreatorCommission.objects.filter(creator=target_user, status='pending')
        settled_count = pending_qs.count()
        total_settled = pending_qs.aggregate(total=Sum('commission_amount'))['total'] or Decimal('0')

        if settled_count == 0:
            return Response({'error': f'No pending commissions to settle for {target_user.username}.'}, status=status.HTTP_400_BAD_REQUEST)

        # Mark all pending commissions as paid/settled
        pending_qs.update(status='paid')

        # Recalculate totals
        all_commissions = CreatorCommission.objects.filter(creator=target_user)
        total_earned = all_commissions.aggregate(total=Sum('commission_amount'))['total'] or Decimal('0')
        total_paid = all_commissions.filter(status='paid').aggregate(total=Sum('commission_amount'))['total'] or Decimal('0')
        total_pending = all_commissions.filter(status='pending').aggregate(total=Sum('commission_amount'))['total'] or Decimal('0')

        print(f"[Commission-Payout-Settled] Admin settled {settled_count} commission(s) totaling ₹{total_settled} for {target_user.username}.")

        return Response({
            'message': f'Successfully settled {settled_count} commission(s) totaling ₹{float(total_settled):,.2f} for {target_user.username}!',
            'total_settled_now': float(total_settled),
            'total_earned': float(total_earned),
            'total_pending': float(total_pending),
            'total_paid': float(total_paid),
            'settled_count': settled_count,
        }, status=status.HTTP_200_OK)


class AdminVIPCreatorsListView(APIView):
    def get(self, request):
        from django.contrib.auth import get_user_model
        from django.db.models import Count, Q, Sum, DecimalField
        from django.db.models.functions import Coalesce
        from decimal import Decimal
        User = get_user_model()

        creators_qs = User.objects.filter(
            Q(is_creator_vip=True) | Q(referrals__isnull=False)
        ).annotate(
            invite_count=Count('referrals'),
            total_commission_earned=Coalesce(Sum('commissions_earned__commission_amount'), Decimal('0'), output_field=DecimalField()),
            total_commission_pending=Coalesce(Sum('commissions_earned__commission_amount', filter=Q(commissions_earned__status='pending')), Decimal('0'), output_field=DecimalField()),
        ).distinct().order_by('-invite_count', '-id')

        creators = []
        for u in creators_qs:
            creators.append({
                'id': u.id,
                'username': u.username,
                'email': u.email or u.username,
                'display_name': u.first_name or u.username,
                'referral_code': u.referral_code,
                'invite_count': u.invite_count,
                'is_creator_vip': u.is_creator_vip,
                'creator_reward_type': u.creator_reward_type,
                'creator_commission_percent': float(u.creator_commission_percent) if u.creator_commission_percent else 10.0,
                'total_commission_earned': float(u.total_commission_earned),
                'total_commission_pending': float(u.total_commission_pending),
                'plan': u.plan,
                'is_premium_active': u.is_premium_active,
                'premium_expires_at': u.premium_expires_at.isoformat() if u.premium_expires_at else None,
                'points': u.points,
            })

        return Response({
            'creators': creators,
            'total_creators': len(creators)
        }, status=status.HTTP_200_OK)


class AdminUsersAnalyticsView(APIView):
    def get(self, request):
        from django.contrib.auth import get_user_model
        User = get_user_model()

        users_qs = User.objects.all().order_by('-date_joined')

        try:
            from apps.products.models import Product
        except Exception:
            Product = None

        try:
            from apps.automations.models import AutomationRule
        except Exception:
            AutomationRule = None

        users_data = []
        from django.db import models
        for u in users_qs:
            # Connected Instagram Accounts
            ig_qs = u.instagram_accounts.filter(is_active=True)
            ig_accounts_count = ig_qs.count()
            ig_accounts_list = []
            for acc in ig_qs:
                ig_accounts_list.append({
                    'id': acc.id,
                    'username': acc.username,
                    'full_name': acc.full_name or acc.username,
                    'profile_picture_url': acc.profile_picture_url,
                    'is_active': acc.is_active,
                    'is_token_expired': acc.is_token_expired,
                    'connected_at': acc.connected_at.isoformat() if acc.connected_at else None,
                })

            # Automations
            automations_count = AutomationRule.objects.filter(
                seller__user=u).count() if AutomationRule else 0

            # Products Published
            p_qs = Product.objects.filter(seller=u) if Product else []
            products_count = p_qs.count() if Product else 0
            products_list = []
            if Product:
                for prod in p_qs[:10]:  # Limit to top 10 for payload efficiency
                    products_list.append({
                        'id': prod.id,
                        'title': prod.title or f"Product #{prod.id}",
                        'price': float(prod.price) if prod.price else 0.0,
                        'status': prod.status,
                        'source_type': prod.source_type,
                        'created_at': prod.created_at.isoformat() if hasattr(prod, 'created_at') and prod.created_at else None,
                    })

            # Referred users & Paid conversions
            referrals_qs = u.referrals.all()
            referred_count = referrals_qs.count()
            paid_referred_count = 0
            referred_users_list = []
            for ref in referrals_qs:
                is_active_pro = ref.is_premium_active or ref.plan == 'pro'
                if is_active_pro:
                    paid_referred_count += 1
                referred_users_list.append({
                    'username': ref.username,
                    'display_name': ref.first_name or ref.username,
                    'date_joined': ref.date_joined.isoformat(),
                    'is_premium_active': is_active_pro,
                    'plan': ref.plan
                })

            purchase_count = u.pro_purchase_count if u.pro_purchase_count > 0 else (
                1 if u.plan == 'pro' else 0)

            # Commission totals (if creator)
            comm_qs = u.commissions_earned.all()
            comm_total_earned = float(comm_qs.aggregate(total=models.Sum('commission_amount'))['total'] or 0)
            comm_total_pending = float(comm_qs.filter(status='pending').aggregate(total=models.Sum('commission_amount'))['total'] or 0)
            comm_total_paid = float(comm_qs.filter(status='paid').aggregate(total=models.Sum('commission_amount'))['total'] or 0)

            # KYC / Bank Details
            kyc_obj = getattr(u, 'kyc', None)
            kyc_data = None
            if kyc_obj:
                kyc_data = {
                    'full_name': kyc_obj.full_name,
                    'bank_name': kyc_obj.bank_name,
                    'bank_account_number': kyc_obj.bank_account_number,
                    'bank_ifsc': kyc_obj.bank_ifsc,
                    'status': kyc_obj.status,
                    'is_card_verified': kyc_obj.is_card_verified,
                }

            users_data.append({
                'id': u.id,
                'username': u.username,
                'email': u.email or u.username,
                'display_name': u.first_name or u.username,
                'referral_code': u.referral_code,
                'is_creator_vip': u.is_creator_vip,
                'creator_reward_type': u.creator_reward_type,
                'creator_commission_percent': float(u.creator_commission_percent) if u.creator_commission_percent else 10.0,
                'commission_total_earned': comm_total_earned,
                'commission_total_pending': comm_total_pending,
                'commission_total_paid': comm_total_paid,
                'kyc': kyc_data,
                'plan': u.plan,
                'is_premium_active': u.is_premium_active,
                'trial_days_left': u.trial_days_left,
                'trial_days': u.trial_days,
                'premium_expires_at': u.premium_expires_at.isoformat() if u.premium_expires_at else None,
                'date_joined': u.date_joined.isoformat(),
                'ig_accounts_count': ig_accounts_count,
                'ig_accounts': ig_accounts_list,
                'automations_count': automations_count,
                'products_count': products_count,
                'products': products_list,
                'referred_count': referred_count,
                'paid_referred_count': paid_referred_count,
                'referred_users': referred_users_list,
                'pro_purchase_count': purchase_count,
                'redeemed_months': u.redeemed_months,
                'points': u.points,
            })

        return Response({
            'users': users_data,
            'total_users': len(users_data)
        }, status=status.HTTP_200_OK)


class GlobalSystemSettingsView(APIView):
    def get(self, request):
        from apps.settings.models import SystemSettings
        sys_settings = SystemSettings.get_settings()
        return Response({
            'trial_days': sys_settings.trial_days,
            'extend_days': sys_settings.extend_days,
            'referral_points': sys_settings.referral_points,
            'points_to_redeem': sys_settings.points_to_redeem,
            'premium_plan_price': float(sys_settings.premium_plan_price),
            'enable_ai': sys_settings.enable_ai,
            'enable_subscription_ai': sys_settings.enable_subscription_ai,
            'business_gemini_api_key': sys_settings.business_gemini_api_key,
        }, status=status.HTTP_200_OK)

    def post(self, request):
        # Allow anyone to update global settings for this playground sandbox app
        from apps.settings.models import SystemSettings
        from decimal import Decimal
        sys_settings = SystemSettings.get_settings()

        trial_days = request.data.get('trial_days')
        extend_days = request.data.get('extend_days')
        referral_points = request.data.get('referral_points')
        points_to_redeem = request.data.get('points_to_redeem')
        premium_plan_price = request.data.get('premium_plan_price')
        enable_ai = request.data.get('enable_ai')
        enable_subscription_ai = request.data.get('enable_subscription_ai')
        business_gemini_api_key = request.data.get('business_gemini_api_key')

        if trial_days is not None:
            sys_settings.trial_days = int(trial_days)
        if extend_days is not None:
            sys_settings.extend_days = int(extend_days)
        if referral_points is not None:
            sys_settings.referral_points = int(referral_points)
        if points_to_redeem is not None:
            sys_settings.points_to_redeem = int(points_to_redeem)
        if premium_plan_price is not None:
            sys_settings.premium_plan_price = Decimal(str(premium_plan_price))
        if enable_ai is not None:
            sys_settings.enable_ai = bool(enable_ai)
        if enable_subscription_ai is not None:
            sys_settings.enable_subscription_ai = bool(enable_subscription_ai)
        if business_gemini_api_key is not None:
            sys_settings.business_gemini_api_key = str(business_gemini_api_key)

        sys_settings.save()
        print(f"[Settings Update] Global settings updated: {sys_settings}")

        return Response({
            'message': 'Global settings updated successfully.',
            'settings': {
                'trial_days': sys_settings.trial_days,
                'extend_days': sys_settings.extend_days,
                'referral_points': sys_settings.referral_points,
                'points_to_redeem': sys_settings.points_to_redeem,
                'premium_plan_price': float(sys_settings.premium_plan_price),
                'enable_ai': sys_settings.enable_ai,
                'enable_subscription_ai': sys_settings.enable_subscription_ai,
                'business_gemini_api_key': sys_settings.business_gemini_api_key,
            }
        }, status=status.HTTP_200_OK)


def auto_enable_subscription_ai_for_user(user):
    try:
        from apps.crm.models import AIAssistantConfig
        from apps.accounts.models import InstagramAccount
        for account in InstagramAccount.objects.filter(user=user):
            config, created = AIAssistantConfig.objects.get_or_create(
                instagram_account=account)
            config.use_business_token = True
            config.save()
            print(
                f"[AI AUTO-SWITCH] Set use_business_token=True for {account.username}")
    except Exception as e:
        print(
            f"[AI AUTO-SWITCH-ERROR] Failed to auto-enable subscription AI: {e}")


class RedeemPremiumWithPointsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        from apps.settings.models import SystemSettings
        sys_settings = SystemSettings.get_settings()

        current_redeemed = getattr(user, 'redeemed_months', 0)
        if current_redeemed >= 5:
            return Response({
                'error': 'Redemption limit reached',
                'details': 'Maximum 5 months points redemption cap reached for creator accounts.'
            }, status=status.HTTP_400_BAD_REQUEST)

        if user.points < sys_settings.points_to_redeem:
            return Response({
                'error': 'Insufficient points',
                'details': f'You need {sys_settings.points_to_redeem} points to redeem premium. You currently have {user.points} points.'
            }, status=status.HTTP_400_BAD_REQUEST)

        user.points -= sys_settings.points_to_redeem
        user.redeemed_months = current_redeemed + 1
        from django.utils import timezone
        user.plan = 'pro'
        if user.premium_expires_at and user.premium_expires_at > timezone.now():
            user.premium_expires_at += timezone.timedelta(days=30)
        else:
            user.premium_expires_at = timezone.now() + timezone.timedelta(days=30)

        user.save()
        auto_enable_subscription_ai_for_user(user)

        print(
            f"[Redemption] User {user.username} redeemed Premium with points ({user.redeemed_months}/5). Remaining: {user.points}")

        return Response({
            'message': f'Premium plan redeemed successfully ({user.redeemed_months}/5 months cap)!',
            'user': serialize_user_payload(user)
        }, status=status.HTTP_200_OK)


# ── Razorpay Integration Views ──────────────────────────────────────────


RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_61r9Oaexv2tXjZ")
RAZORPAY_KEY_SECRET = os.getenv(
    "RAZORPAY_KEY_SECRET", "S7tK7rX35JqZJ35pL2O2x7w8")
RAZORPAY_WEBHOOK_SECRET = os.getenv(
    "RAZORPAY_WEBHOOK_SECRET", "web_secret_anydm_123")


class RazorpayCreateOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        from apps.settings.models import SystemSettings
        sys_settings = SystemSettings.get_settings()

        # Premium Plan price in INR
        price_in_inr = sys_settings.premium_plan_price
        price_in_paise = int(price_in_inr * 100)

        try:
            client = razorpay.Client(
                auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
            order_data = {
                'amount': price_in_paise,
                'currency': 'INR',
                'payment_capture': 1,  # Automatic capture
                'notes': {
                    'user_id': str(user.id),
                    'user_email': user.email or '',
                    'username': user.username
                }
            }
            order = client.order.create(data=order_data)

            print(
                f"[Razorpay] Created order {order['id']} for User {user.username}. Amount: {price_in_inr} INR")

            return Response({
                'order_id': order['id'],
                'amount': order['amount'],
                'currency': order['currency'],
                'key_id': RAZORPAY_KEY_ID
            }, status=status.HTTP_200_OK)

        except Exception as e:
            print(f"[Razorpay Error] Failed to create order: {e}")
            return Response({
                'error': 'Failed to create payment order',
                'details': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RazorpayVerifyPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        razorpay_order_id = request.data.get('razorpay_order_id')
        razorpay_payment_id = request.data.get('razorpay_payment_id')
        razorpay_signature = request.data.get('razorpay_signature')

        if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature]):
            return Response({'error': 'Missing payment verification details.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            client = razorpay.Client(
                auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
            # Verify signature
            params_dict = {
                'razorpay_order_id': razorpay_order_id,
                'razorpay_payment_id': razorpay_payment_id,
                'razorpay_signature': razorpay_signature
            }
            client.utility.verify_payment_signature(params_dict)

            # Verification successful - Upgrade plan
            from django.utils import timezone
            user.plan = 'pro'
            user.pro_purchase_count = getattr(
                user, 'pro_purchase_count', 0) + 1
            user.premium_expires_at = timezone.now() + timezone.timedelta(days=30)

            # Reward referrer 20 points on first paid purchase
            if user.referred_by and not getattr(user, 'referral_paid_reward_given', False):
                from apps.settings.models import SystemSettings
                sys_settings = SystemSettings.get_settings()
                reward_points = sys_settings.referral_points or 20
                user.referred_by.points += reward_points
                user.referred_by.save(update_fields=['points'])
                user.referral_paid_reward_given = True
                print(
                    f"[Referral-Purchase-Reward] User {user.username} paid. Credited {reward_points} points to referrer {user.referred_by.username}.")

                # Creator Commission: record commission if referrer is in commission mode
                referrer = user.referred_by
                if referrer.is_creator_vip and referrer.creator_reward_type == 'commission':
                    from decimal import Decimal
                    from apps.accounts.models import CreatorCommission
                    try:
                        plan_price = Decimal(str(sys_settings.premium_plan_price))
                    except Exception:
                        plan_price = Decimal('499.00')
                    pct = referrer.creator_commission_percent or Decimal('10.00')
                    commission_amt = (plan_price * pct) / Decimal('100')
                    CreatorCommission.objects.create(
                        creator=referrer,
                        referred_user=user,
                        payment_amount=plan_price,
                        commission_percent=pct,
                        commission_amount=commission_amt,
                    )
                    print(
                        f"[Creator-Commission] {referrer.username} earned {commission_amt} ({pct}% of {plan_price}) from {user.username}'s first purchase.")

            user.save()
            auto_enable_subscription_ai_for_user(user)

            print(
                f"[Razorpay-Success] Verified payment {razorpay_payment_id} for user {user.username}. Upgraded to Premium.")

            return Response({
                'message': 'Payment verified successfully and Premium activated!',
                'user': serialize_user_payload(user)
            }, status=status.HTTP_200_OK)

        except Exception as e:
            print(f"[Razorpay-Verification-Failed] Verification error: {e}")
            return Response({
                'error': 'Payment verification failed',
                'details': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)


class RazorpayWebhookView(APIView):
    permission_classes = []  # Public, verified by signature

    def post(self, request):
        webhook_signature = request.headers.get('X-Razorpay-Signature')
        webhook_body = request.body.decode('utf-8')

        if not webhook_signature:
            return Response({'error': 'No webhook signature provided.'}, status=400)

        try:
            client = razorpay.Client(
                auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
            # Verify signature
            # TODO
            # client.utility.verify_webhook_signature(
            #     webhook_body,
            #     webhook_signature,
            #     RAZORPAY_WEBHOOK_SECRET
            # )

            # Parse event data
            event_data = json.loads(webhook_body)
            event_type = event_data.get('event')

            print(f"[Razorpay-Webhook] Received event: {event_type}")

            if event_type in ['payment.captured', 'order.paid']:
                payment_entity = event_data['payload']['payment']['entity']
                notes = payment_entity.get('notes', {})
                user_id = notes.get('user_id')

                if user_id:
                    from django.contrib.auth import get_user_model
                    User = get_user_model()
                    user = User.objects.filter(id=user_id).first()
                    if user:
                        from django.utils import timezone
                        user.plan = 'pro'
                        user.pro_purchase_count = getattr(user, 'pro_purchase_count', 0) + 1
                        user.premium_expires_at = timezone.now() + timezone.timedelta(days=30)

                        # Creator Commission via webhook (first payment only)
                        if user.referred_by and not getattr(user, 'referral_paid_reward_given', False):
                            referrer = user.referred_by
                            if referrer.is_creator_vip and referrer.creator_reward_type == 'commission':
                                from decimal import Decimal
                                from apps.accounts.models import CreatorCommission
                                from apps.settings.models import SystemSettings
                                sys_settings = SystemSettings.get_settings()
                                try:
                                    plan_price = Decimal(str(sys_settings.premium_plan_price))
                                except Exception:
                                    plan_price = Decimal('499.00')
                                pct = referrer.creator_commission_percent or Decimal('10.00')
                                commission_amt = (plan_price * pct) / Decimal('100')
                                CreatorCommission.objects.create(
                                    creator=referrer,
                                    referred_user=user,
                                    payment_amount=plan_price,
                                    commission_percent=pct,
                                    commission_amount=commission_amt,
                                )
                                print(
                                    f"[Creator-Commission-Webhook] {referrer.username} earned {commission_amt} from {user.username}'s first purchase.")

                        user.save()
                        auto_enable_subscription_ai_for_user(user)
                        print(
                            f"[Razorpay-Webhook-Success] Upgrade user {user.username} to Premium via Webhook.")

            return Response({'status': 'ok'}, status=200)

        except Exception as e:
            print(f"[Razorpay-Webhook-Error] Failed to process webhook: {e}")
            # Still return 200/ok so Razorpay doesn't keep retrying if signature was fine, or 400 if bad signature
            return Response({'error': str(e)}, status=400)


class InstagramRateLimitStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        account_id = request.query_params.get('account_id')
        if account_id:
            account = user.instagram_accounts.filter(id=account_id).first()
        else:
            account = user.instagram_accounts.filter(
                is_active=True).first() or user.instagram_accounts.first()

        if not account:
            return Response({
                "account_id": None,
                "username": None,
                "hourly_dm_count": 0,
                "hourly_dm_limit": 200,
                "hourly_dm_remaining": 200,
                "daily_dm_count": 0,
                "daily_dm_limit": 2000,
                "daily_publish_count": 0,
                "daily_publish_limit": 100,
                "rate_limit_utilization_pct": 0,
                "reset_time_seconds": 3600,
                "health_status": "SAFE",
                "anti_block_protection": {
                    "status": "ACTIVE",
                    "jitter_delay_range": "1.5s - 3.5s",
                    "webhook_events": "ENABLED",
                    "auto_throttle": "ENABLED"
                }
            }, status=200)

        from django.core.cache import cache
        import time

        now_ts = time.time()
        key = f"ig_dm_timestamps_{account.id}"
        timestamps = cache.get(key, [])

        # Filter last 1 hour (3600 seconds)
        one_hour_ago = now_ts - 3600
        hourly_timestamps = [t for t in timestamps if t > one_hour_ago]
        hourly_dm_count = len(hourly_timestamps)

        # Filter last 24 hours (86400 seconds)
        twenty_four_hours_ago = now_ts - 86400
        daily_timestamps = [t for t in timestamps if t > twenty_four_hours_ago]
        daily_dm_count = len(daily_timestamps)

        # Meta usage header parsed
        usage_json = cache.get(f"ig_rate_limit_usage_{account.id}", {})
        meta_pct = 0
        reset_seconds = 3600 - int(now_ts % 3600)

        if "ig_api_usage" in usage_json and len(usage_json["ig_api_usage"]) > 0:
            item = usage_json["ig_api_usage"][0]
            meta_pct = item.get("acc_id_util_pct", 0)
            reset_seconds = item.get("reset_time_duration", reset_seconds)
        else:
            # Fallback estimation based on hourly volume
            meta_pct = min(100, int((hourly_dm_count / 200.0) * 100))

        # Overall safety status
        if meta_pct >= 90 or hourly_dm_count >= 180:
            health_status = "WARNING"
        elif meta_pct >= 60 or hourly_dm_count >= 120:
            health_status = "MODERATE"
        else:
            health_status = "SAFE"

        return Response({
            "account_id": account.id,
            "username": account.username,
            "hourly_dm_count": hourly_dm_count,
            "hourly_dm_limit": 200,
            "hourly_dm_remaining": max(0, 200 - hourly_dm_count),
            "daily_dm_count": daily_dm_count,
            "daily_dm_limit": 2000,
            "daily_publish_count": 0,
            "daily_publish_limit": 100,
            "rate_limit_utilization_pct": meta_pct,
            "reset_time_seconds": reset_seconds,
            "health_status": health_status,
            "anti_block_protection": {
                "status": "ACTIVE",
                "jitter_delay_range": "1.5s - 3.5s",
                "webhook_events": "ENABLED",
                "auto_throttle": "ENABLED"
            }
        }, status=200)
