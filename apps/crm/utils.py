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

        instagram_id = (
            customer.instagram_user_id
            or customer.instagram_scoped_id
        )

        url = f"https://graph.instagram.com/v25.0/{instagram_id}"

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
        if e.response is not None and e.response.status_code >= 500:
            logger.warning(f"Instagram API Server Error for customer {customer.id} (Status {e.response.status_code}). Skipping sync.")
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
        attachments = interaction.metadata.get("attachments")
        
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
        if not interaction.seller_account or not interaction.seller_account.user:
            logger.warning(f"Unable to broadcast interaction {interaction.id}. Missing seller account or owner user.")
            return
        
        user_id = interaction.seller_account.user.id
        group_name = f"user_{user_id}"
        
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