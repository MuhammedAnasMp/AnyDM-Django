"""
Instagram Content Publishing Engine v25.0
Handles creating media containers, polling status for videos/reels, publishing containers,
and retrieving live Instagram permalinks.
"""

import time
import logging
import requests
from django.utils import timezone

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v25.0"
BASE_URL = f"https://graph.instagram.com/{GRAPH_API_VERSION}"


def create_media_container(account, post_type, media_url, caption="", cover_url=None, share_to_feed=True, carousel_urls=None):
    """
    Step 1: Create a media creation container with Meta Graph API.
    Supports REELS, IMAGE, VIDEO, STORIES, CAROUSEL.
    Returns container_id or raises an Exception.
    """
    access_token = account.access_token
    instagram_user_id = account.instagram_scoped_id or account.instagram_user_id

    if not access_token or not instagram_user_id:
        raise ValueError(f"Account {account.id} missing access token or Instagram user ID.")

    url = f"{BASE_URL}/{instagram_user_id}/media"
    params = {"access_token": access_token}

    is_media_video = any(media_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi', '.webm', '.mkv']) or '/video/upload/' in media_url

    if post_type in ["REELS", "VIDEO"] and not is_media_video:
        logger.warning(f"[PUBLISHER] post_type was '{post_type}' but media URL is an image. Switching post_type to 'IMAGE'.")
        post_type = "IMAGE"
    elif post_type == "IMAGE" and is_media_video:
        logger.warning(f"[PUBLISHER] post_type was 'IMAGE' but media URL is a video. Switching post_type to 'REELS'.")
        post_type = "REELS"

    if post_type == "IMAGE":
        data = {
            "image_url": media_url,
            "caption": caption
        }
    elif post_type in ["REELS", "VIDEO"]:
        data = {
            "media_type": "REELS",
            "video_url": media_url,
            "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false"
        }
        if cover_url:
            data["cover_url"] = cover_url
    elif post_type == "STORIES":
        is_video = is_media_video or media_url.lower().endswith(('.mp4', '.mov', '.avi', '.webm')) or 'video' in media_url
        data = {
            "media_type": "STORIES",
            ("video_url" if is_video else "image_url"): media_url
        }
    elif post_type == "CAROUSEL":
        valid_items = [u for u in (carousel_urls or []) if u][:10]
        if len(valid_items) < 2:
            raise ValueError("Carousel posts require at least 2 media items.")
        
        children_ids = []
        for item_url in valid_items:
            is_vid = any(item_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi', '.webm', '.mkv']) or '/video/upload/' in item_url
            child_data = {
                "is_carousel_item": "true",
                "media_type": "VIDEO" if is_vid else "IMAGE",
                ("video_url" if is_vid else "image_url"): item_url
            }
            child_res = requests.post(url, params=params, data=child_data, timeout=30)
            child_json = child_res.json()
            if "id" not in child_json:
                error_detail = child_json.get("error", {}).get("message", str(child_json))
                raise Exception(f"Failed to create carousel item for {item_url}: {error_detail}")
            child_id = child_json["id"]
            if is_vid:
                poll_container_status(account, child_id)
            children_ids.append(child_id)
            
        data = {
            "media_type": "CAROUSEL",
            "children": ",".join(children_ids),
            "caption": caption
        }
    else:
        raise ValueError(f"Unsupported post_type: {post_type}")

    logger.info(f"[PUBLISHER] Creating container for {post_type} on @{account.username}")
    res = requests.post(url, params=params, data=data, timeout=30)
    res_data = res.json()

    if "id" not in res_data:
        error_msg = res_data.get("error", {}).get("message", str(res_data))
        logger.error(f"[PUBLISHER] Failed to create container: {error_msg}")
        raise Exception(f"Failed to create Instagram container: {error_msg}")

    container_id = res_data["id"]
    logger.info(f"[PUBLISHER] Container created successfully: {container_id}")
    return container_id


def poll_container_status(account, container_id, max_attempts=40, delay_seconds=3):
    """
    Step 2: Poll container status_code until FINISHED or ERROR.
    """
    access_token = account.access_token
    status_url = f"{BASE_URL}/{container_id}"
    params = {"access_token": access_token, "fields": "status_code,status"}

    for attempt in range(max_attempts):
        time.sleep(delay_seconds)
        res = requests.get(status_url, params=params, timeout=15)
        stat_data = res.json()
        status_code = stat_data.get("status_code")
        logger.info(f"[PUBLISHER] Polling container {container_id} (Attempt {attempt+1}/{max_attempts}): {status_code} - Data: {stat_data}")

        if status_code == "FINISHED":
            return True
        elif status_code == "ERROR":
            err = stat_data.get("status") or stat_data.get("error", {}).get("message") or "Container processing failed on Meta."
            raise Exception(f"Meta container error: {err}")
        elif status_code == "EXPIRED":
            raise Exception("Meta container creation expired.")

    raise TimeoutError(f"Container processing timed out for ID {container_id} after {max_attempts * delay_seconds}s.")


def publish_container(account, container_id, max_retries=6, retry_delay=3):
    """
    Step 3: Publish the ready container to Instagram with auto-retry if Meta is still downloading media.
    Returns published media_id or raises an Exception.
    """
    access_token = account.access_token
    instagram_user_id = account.instagram_scoped_id or account.instagram_user_id
    publish_url = f"{BASE_URL}/{instagram_user_id}/media_publish"
    params = {"access_token": access_token}
    data = {"creation_id": container_id}

    for attempt in range(max_retries):
        logger.info(f"[PUBLISHER] Publishing container {container_id} for @{account.username} (Attempt {attempt+1}/{max_retries})")
        res = requests.post(publish_url, params=params, data=data, timeout=30)
        res_data = res.json()

        if "id" in res_data:
            media_id = res_data["id"]
            logger.info(f"[PUBLISHER] Successfully published to Instagram! Media ID: {media_id}")
            return media_id

        error_msg = res_data.get("error", {}).get("message", str(res_data))
        # If Meta is still finalizing or downloading the media asset from CDN
        if any(keyword in error_msg.lower() for keyword in ["not available", "not ready", "in progress", "loading"]):
            logger.warning(f"[PUBLISHER] Container {container_id} not ready yet ({error_msg}). Retrying in {retry_delay}s...")
            time.sleep(retry_delay)
            continue

        logger.error(f"[PUBLISHER] Publishing failed: {error_msg}")
        raise Exception(f"Publishing failed: {error_msg}")

    raise Exception(f"Publishing failed: Container {container_id} was not ready after {max_retries * retry_delay}s.")


def fetch_media_permalink(account, media_id):
    """
    Step 4: Fetch permalink for the published media.
    """
    try:
        access_token = account.access_token
        url = f"{BASE_URL}/{media_id}"
        params = {"access_token": access_token, "fields": "permalink,media_type,thumbnail_url"}
        res = requests.get(url, params=params, timeout=15)
        res_data = res.json()
        return res_data.get("permalink")
    except Exception as e:
        logger.warning(f"[PUBLISHER] Failed to fetch permalink for media {media_id}: {e}")
        return None


def broadcast_scheduled_post_update(scheduled_post, event_action="status_update"):
    """
    Broadcasts real-time WebSocket update to the seller's Instagram channel and user channel.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from apps.automations.views import ScheduledPostSerializer

        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        payload = {
            "event_type": "scheduled_post_update",
            "action": event_action,
            "post": ScheduledPostSerializer(scheduled_post).data
        }

        # Send to user group
        if scheduled_post.user_id:
            async_to_sync(channel_layer.group_send)(
                f"user_{scheduled_post.user_id}",
                {
                    "type": "chat_message",
                    "payload": payload
                }
            )

        # Send to instagram account group
        if scheduled_post.seller_id:
            async_to_sync(channel_layer.group_send)(
                f"instagram_{scheduled_post.seller_id}",
                {
                    "type": "chat_message",
                    "payload": payload
                }
            )
            # If seller has an associated user
            if scheduled_post.seller.user_id and scheduled_post.seller.user_id != scheduled_post.user_id:
                async_to_sync(channel_layer.group_send)(
                    f"user_{scheduled_post.seller.user_id}",
                    {
                        "type": "chat_message",
                        "payload": payload
                    }
                )

        logger.info(f"[WS-BROADCAST] Post #{scheduled_post.id} status '{scheduled_post.status}' broadcasted via WebSocket.")
    except Exception as ws_err:
        logger.warning(f"[WS-BROADCAST] Failed to broadcast WebSocket update for post #{scheduled_post.id}: {ws_err}")


def execute_publish_post(scheduled_post):
    """
    Orchestrates the complete publish pipeline for a ScheduledPost record.
    Updates model fields, status, permalinks, and syncs linked products.
    """
    account = scheduled_post.seller
    scheduled_post.status = "PROCESSING"
    scheduled_post.save(update_fields=['status'])
    broadcast_scheduled_post_update(scheduled_post, "processing")

    try:
        # Step 1: Create Container
        container_id = create_media_container(
            account=account,
            post_type=scheduled_post.post_type,
            media_url=scheduled_post.media_url,
            caption=scheduled_post.caption or "",
            cover_url=scheduled_post.cover_url,
            share_to_feed=scheduled_post.share_to_feed,
            carousel_urls=scheduled_post.carousel_urls
        )
        scheduled_post.container_id = container_id
        scheduled_post.save(update_fields=['container_id'])

        # Step 2: Poll container readiness (only required for video/reels or video stories)
        is_vid = any(scheduled_post.media_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi', '.webm', '.mkv']) or '/video/upload/' in scheduled_post.media_url
        if scheduled_post.post_type in ["REELS", "VIDEO"] or (scheduled_post.post_type == "STORIES" and is_vid):
            poll_container_status(account, container_id, max_attempts=45, delay_seconds=4)
        else:
            # Single image containers are ready immediately on Meta Graph API
            time.sleep(1)

        # Step 3: Publish Container with auto-retry
        media_id = publish_container(account, container_id)
        scheduled_post.instagram_media_id = media_id

        # Step 4: Fetch Permalink
        permalink = fetch_media_permalink(account, media_id)
        scheduled_post.instagram_permalink = permalink

        scheduled_post.status = "PUBLISHED"
        scheduled_post.published_at = timezone.now()
        scheduled_post.error_message = None
        scheduled_post.save(update_fields=[
            'status', 'instagram_media_id', 'instagram_permalink', 'published_at', 'error_message'
        ])

        # Step 5: If linked to a product, sync product Instagram fields
        if scheduled_post.product:
            try:
                prod = scheduled_post.product
                prod.media_id = media_id
                prod.instagram_permalink = permalink
                if permalink:
                    from apps.products.utils import extract_instagram_id
                    shortcode = extract_instagram_id(permalink)
                    if shortcode:
                        prod.source_id = shortcode
                prod.save(update_fields=['media_id', 'instagram_permalink', 'source_id'])
                logger.info(f"[PUBLISHER] Linked Product #{prod.id} with published Instagram media #{media_id}")
            except Exception as prod_err:
                logger.error(f"[PUBLISHER] Error syncing Product #{scheduled_post.product.id}: {prod_err}")

        # Step 6: Auto-create Comment-to-DM Automation Rule for this specific published media
        auto_cfg = scheduled_post.automation_config or {}
        if auto_cfg and auto_cfg.get('enabled', True):
            try:
                from apps.automations.models import AutomationRule, AutomationAction
                
                rule_type = 'story_automation' if scheduled_post.post_type == 'STORIES' else 'comment_automation'
                target_media_type = 'reel' if scheduled_post.post_type in ['REELS', 'VIDEO'] else ('story' if scheduled_post.post_type == 'STORIES' else 'post')
                custom_cover = scheduled_post.cover_url
                rule_name = auto_cfg.get('rule_name') or f"Auto DM for {scheduled_post.product.title if scheduled_post.product else 'Post'}"
                
                raw_keywords = auto_cfg.get('keywords', ['PRICE', 'BUY', 'LINK'])
                if isinstance(raw_keywords, str):
                    keywords = [k.strip().upper() for k in raw_keywords.split(',') if k.strip()]
                elif isinstance(raw_keywords, list):
                    keywords = [str(k).strip().upper() for k in raw_keywords if str(k).strip()]
                else:
                    keywords = ['PRICE', 'BUY', 'LINK']

                match_type = auto_cfg.get('match_type', 'contains')
                if match_type not in ['contains', 'equals', 'any']:
                    match_type = 'contains'

                follower_gate_enabled = bool(auto_cfg.get('follower_gate', False))
                raw_fg_msgs = auto_cfg.get('follower_gate_messages', ["Please follow our page to unlock this offer! ✨"])
                follower_gate_messages = raw_fg_msgs if isinstance(raw_fg_msgs, list) else [str(raw_fg_msgs)]

                # Create AutomationRule scoped specifically to this media_id
                rule = AutomationRule.objects.create(
                    seller=account,
                    name=rule_name,
                    rule_type=rule_type,
                    status='active',
                    target_mode='selected',
                    target_media_ids=[str(media_id)],
                    target_media_type=target_media_type,
                    condition_match_type=match_type,
                    condition_keywords=keywords,
                    follower_gate_enabled=follower_gate_enabled,
                    follower_gate_messages=follower_gate_messages
                )

                # Prepare DM Action (Rich Product Card by default or Plain Text)
                dm_text = auto_cfg.get('dm_message')
                dm_format = auto_cfg.get('dm_format', 'generic_template')
                generic_payload = {}

                if scheduled_post.product and dm_format == 'generic_template':
                    prod = scheduled_post.product
                    store_link = f"https://app.zoyee.in/{account.username}/product/{prod.id}"
                    
                    # Ensure a valid image URL is used for Instagram Generic Template card
                    card_img = auto_cfg.get('card_image_url') or custom_cover or getattr(prod, 'thumbnail_url', None)
                    if not card_img:
                        img_media = prod.gallery.filter(media_type='IMAGE').first()
                        if img_media and img_media.media_url:
                            card_img = img_media.media_url
                        elif prod.main_media_url and not any(prod.main_media_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.webm', '.mkv']):
                            card_img = prod.main_media_url
                        else:
                            card_img = prod.main_media_url or "https://app.zoyee.in/favicon.ico"

                    price_str = f"{prod.currency or '₹'}{prod.price:g}" if isinstance(prod.price, (int, float)) else f"{prod.currency or '₹'}{prod.price}"
                    
                    generic_payload = {
                        "elements": [
                            {
                                "title": prod.title[:80],
                                "subtitle": f"Price: {price_str} • Tap below to purchase",
                                "image_url": card_img,
                                "default_action": {
                                    "type": "web_url",
                                    "url": store_link
                                },
                                "buttons": [
                                    {
                                        "type": "web_url",
                                        "url": store_link,
                                        "title": "Buy Now 🛍️"
                                    }
                                ]
                            }
                        ]
                    }
                    if not dm_text:
                        dm_text = f"Hey! Thanks for your comment! Here is the direct link to purchase {prod.title} for {price_str}:\n{store_link}"
                else:
                    if not dm_text:
                        if scheduled_post.product:
                            prod = scheduled_post.product
                            store_link = f"https://app.zoyee.in/{account.username}/product/{prod.id}"
                            dm_text = f"Hey! Thanks for your comment! Here is the direct link to purchase {prod.title} for {prod.currency or '₹'}{prod.price}:\n{store_link}"
                        else:
                            dm_text = "Hey! Thanks for your comment on our post! Check your inbox for product details."
                    dm_format = 'text'

                # Action 1: Send DM (Product Card or Text)
                AutomationAction.objects.create(
                    rule=rule,
                    order=0,
                    action_type='send_dm',
                    dm_format=dm_format,
                    messages=[dm_text],
                    generic_template_payload=generic_payload,
                    linked_product=scheduled_post.product
                )

                # Action 2: Public Comment Reply (if rule is comment automation)
                raw_replies = auto_cfg.get('comment_replies') or [auto_cfg.get('comment_reply')] or ["Sent you a DM with the product link! 🛍️ Check your inbox!"]
                if isinstance(raw_replies, str):
                    comment_replies = [r.strip() for r in raw_replies.split('\n') if r.strip()]
                elif isinstance(raw_replies, list):
                    comment_replies = [str(r).strip() for r in raw_replies if str(r).strip()]
                else:
                    comment_replies = ["Sent you a DM with the product link! 🛍️ Check your inbox!"]

                if rule_type == 'comment_automation' and comment_replies:
                    AutomationAction.objects.create(
                        rule=rule,
                        order=1,
                        action_type='reply_comment',
                        messages=comment_replies
                    )

                # Generate React Flow visual_data (nodes and edges) for the canvas builder
                try:
                    from apps.automations.views import build_visual_data_from_rule
                    build_visual_data_from_rule(rule)
                except Exception as ve:
                    logger.warning(f"[PUBLISHER] Non-critical: Failed to pre-generate visual_data for rule #{rule.id}: {ve}")

                logger.info(f"[PUBLISHER] Successfully auto-created AutomationRule #{rule.id} ('{rule_name}') targeting media #{media_id}")
            except Exception as auto_err:
                logger.error(f"[PUBLISHER] Error creating auto-DM rule for post #{scheduled_post.id}: {auto_err}")

        # Real-time WebSocket broadcast
        broadcast_scheduled_post_update(scheduled_post, "published")

        logger.info(f"[PUBLISHER] Post #{scheduled_post.id} completed successfully! Link: {permalink}")
        return True, permalink

    except Exception as e:
        error_msg = str(e)
        logger.error(f"[PUBLISHER] Execution failed for post #{scheduled_post.id}: {error_msg}")
        scheduled_post.status = "FAILED"
        scheduled_post.error_message = error_msg
        scheduled_post.save(update_fields=['status', 'error_message'])
        # Real-time WebSocket broadcast
        broadcast_scheduled_post_update(scheduled_post, "failed")
        return False, error_msg
