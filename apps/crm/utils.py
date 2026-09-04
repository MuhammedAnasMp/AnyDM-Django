import requests
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


def sync_customer_profile(customer, force=False):
    """
    Sync Instagram user information into Customer.
    """
    print("customer details fetching............................")
    if not force:
        needs_sync = any([
            not customer.username,
            not customer.full_name,
            not customer.profile_pic,
            customer.is_following_business is None,
        ])

        if not needs_sync:
            return customer

    try:
        account = customer.owner

        if not account.access_token:
            logger.warning(
                f"Missing access token for account {account.id}"
            )
            return customer

        # Cross-reference existing customer records for the same seller by username
        if customer.username:
            from apps.crm.models import Customer
            same_user_cust = Customer.objects.filter(
                owner=account,
                username__iexact=customer.username
            ).exclude(id=customer.id).order_by('-id').first()

            if same_user_cust:
                changed = False
                if not customer.instagram_user_id and same_user_cust.instagram_user_id:
                    customer.instagram_user_id = same_user_cust.instagram_user_id
                    changed = True
                if customer.is_following_business is None and same_user_cust.is_following_business is not None:
                    customer.is_following_business = same_user_cust.is_following_business
                    changed = True
                if not customer.full_name and same_user_cust.full_name:
                    customer.full_name = same_user_cust.full_name
                    changed = True
                if not customer.profile_pic and same_user_cust.profile_pic:
                    customer.profile_pic = same_user_cust.profile_pic
                    changed = True

                if changed:
                    customer.save(update_fields=['instagram_user_id', 'is_following_business', 'full_name', 'profile_pic'])

        instagram_id = (
            customer.instagram_user_id
            or customer.instagram_scoped_id
        )

        if not instagram_id:
            logger.warning(f"No instagram_id found for customer {customer.id}")
            return customer

        # Prevent syncing self business account profile
        owner_ids = [
            str(getattr(account, 'instagram_account_id', '')),
            str(getattr(account, 'instagram_scoped_id', '')),
            str(getattr(account, 'instagram_user_id', '')),
        ]
        if str(instagram_id) in owner_ids:
            logger.info(f"Customer {customer.id} ID {instagram_id} matches seller account ID. Skipping self-sync.")
            return customer

        url = f"https://graph.instagram.com/v26.0/{instagram_id}"

        params = {
            "fields": ",".join([
                "name",
                "username",
                "profile_pic",
                "follower_count",
                "is_user_follow_business",
                "is_business_follow_user",
            ]),
            "access_token": account.access_token,
        }

        response = requests.get(
            url,
            params=params,
            timeout=15
        )

        if response.status_code != 200:
            try:
                err_body = response.json().get("error", {})
                err_code = err_body.get("code")
                err_type = err_body.get("type", "")
                err_msg = err_body.get("message", "")

                if err_code == 230 or "User consent is required" in err_msg:
                    logger.info(
                        f"User consent required for Customer {customer.id} (IGSID: {instagram_id}). "
                        "Profile access restricted by Instagram until user messages business directly."
                    )
                    return customer

                if response.status_code in [401, 403] or err_code == 190 or err_type == "OAuthException":
                    account.is_token_expired = True
                    account.save(update_fields=['is_token_expired'])
                    logger.warning(f"Access token for account {account.id} marked as expired due to OAuth error (code {err_code}).")
                    return customer

                if err_code == 100:
                    logger.info(f"Instagram user profile for customer {customer.id} (IGSID: {instagram_id}) not found.")
                    return customer
            except Exception:
                pass

        response.raise_for_status()

        data = response.json()

        update_fields = []

        # Instagram IDs
        if not customer.instagram_user_id:
            customer.instagram_user_id = data.get("id")
            update_fields.append("instagram_user_id")

        # Name
        if data.get("name") and customer.full_name != data["name"]:
            customer.full_name = data["name"]
            update_fields.append("full_name")

        # Username
        if data.get("username") and customer.username != data["username"]:
            customer.username = data["username"]
            update_fields.append("username")

        # Profile picture
        if data.get("profile_pic") and customer.profile_pic != data["profile_pic"]:
            customer.profile_pic = data["profile_pic"]
            update_fields.append("profile_pic")

        # Follow relationship (Business follows user)
        if "is_user_follow_business" in data:
            customer.is_following_business = data["is_user_follow_business"]
            update_fields.append("is_following_business")

            if (
                data["is_user_follow_business"]
                and customer.followed_at is None
            ):
                customer.followed_at = timezone.now()
                update_fields.append("followed_at")
                
        # Corrected field mapping to match customer.is_business_follow_user in models.py
        if "is_business_follow_user" in data:
            customer.is_business_follow_user = data["is_business_follow_user"]
            update_fields.append("is_business_follow_user")

        if update_fields:
            customer.save(update_fields=list(set(update_fields)))

            logger.info(
                f"Customer {customer.id} synced successfully"
            )

        return customer

    except requests.exceptions.RequestException as e:
        if e.response is not None:
            if e.response.status_code in [401, 403]:
                account.is_token_expired = True
                account.save(update_fields=['is_token_expired'])
            if e.response.status_code >= 500:
                logger.warning(f"Instagram API Server Error for customer {customer.id} (Status {e.response.status_code}). Skipping sync.")
            else:
                logger.error(f"API Error syncing customer {customer.id}: {e}")
        else:
            logger.error(f"API Error syncing customer {customer.id}: {e}")
    except Exception as e:
        logger.error(f"Failed syncing customer {customer.id}: {e}", exc_info=True)

    return customer


def send_to_group(group_name, event):
    """
    Sends an event to a Django Channels group using get_channel_layer and async_to_sync.
    Useful for publishing messages from Celery workers or views without opening WS connections.
    """
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    import logging
    
    logger = logging.getLogger(__name__)
    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            async_to_sync(channel_layer.group_send)(group_name, event)
            logger.info(f"Successfully sent event to group {group_name}: {event}")
        except Exception as e:
            logger.error(f"Error sending event to group {group_name}: {e}")
    else:
        logger.warning(f"No channel layer configured. Event not sent to group {group_name}.")


def serialize_interaction_to_message(interaction):
    """
    Serializes a CustomerInteraction object into a dictionary format compatible with the frontend.
    """
    is_inbound = interaction.direction == "INBOUND"
    
    if is_inbound:
        from_user = {
            "id": interaction.customer.instagram_scoped_id,
            "username": interaction.customer.username or "Instagram User"
        }
        to_user = {
            "id": interaction.seller_account.instagram_scoped_id or interaction.seller_account.instagram_user_id,
            "username": interaction.seller_account.username
        }
    else:
        from_user = {
            "id": interaction.seller_account.instagram_scoped_id or interaction.seller_account.instagram_user_id,
            "username": interaction.seller_account.username
        }
        to_user = {
            "id": interaction.customer.instagram_scoped_id,
            "username": interaction.customer.username or "Instagram User"
        }
        
    attachments = None
    if interaction.metadata and isinstance(interaction.metadata, dict):
        # Check direct attachments first (inbound messages)
        attachments = interaction.metadata.get("attachments")
        
        # For outbound template messages, extract from sent_payload
        if not attachments:
            sent_payload = interaction.metadata.get("sent_payload", {})
            msg = sent_payload.get("message", {}) if sent_payload else {}
            att = msg.get("attachment") if msg else None
            if att:
                # Wrap in list so it's consistent with how the frontend expects attachments.data
                attachments = [att]

    return {
        "id": interaction.instagram_event_id or f"temp_{interaction.id}",
        "from": from_user,
        "to": to_user,
        "message": interaction.message_text or "",
        "created_time": (interaction.platform_timestamp or interaction.created_at).isoformat(),
        "attachments": attachments,
        "message_source": interaction.message_source
    }


def broadcast_interaction(interaction):
    """
    Helper to broadcast a CustomerInteraction message to the owner user's channel group.
    """
    import logging
    logger = logging.getLogger(__name__)
    try:
        if not interaction.seller_account:
            logger.warning(f"Unable to broadcast interaction {interaction.id}. Missing seller account.")
            return
        
        group_name = f"instagram_{interaction.seller_account.id}"
        
        serialized_msg = serialize_interaction_to_message(interaction)
        event = {
            "type": "chat.message",
            "payload": {
                "event_type": "new_message",
                "recipient_id": interaction.customer.instagram_scoped_id,
                "message": serialized_msg
            }
        }
        send_to_group(group_name, event)
    except Exception as e:
        logger.error(f"Failed to broadcast interaction {interaction.id}: {e}", exc_info=True)