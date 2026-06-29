from django.utils import module_loading
import json
import hashlib
import hmac
import datetime
import logging
import re
from urllib.parse import urlparse, parse_qs

from django.conf import settings
from django.http import HttpResponse
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db.models import Q
from django.db import connection, transaction, IntegrityError
from django.core.cache import cache

from apps.accounts.models import InstagramAccount
from apps.products.models import Product
from .models import CustomerInteraction, Customer, Enquiry, EnquiryProduct
from .tasks import sync_customer_profile_task

# Safe import and compilation stub definition to avoid NameErrors
try:
    from .tasks import process_enquiry_background_task
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    def process_enquiry_background_task(*args, **kwargs):
        pass

logger = logging.getLogger(__name__)


def extract_media_id_safely(event_type, metadata):
    """
    Defensively extracts Instagram item/media IDs across various payload structures,
    including comments, stories, and shared reels/posts in direct messages.
    """
    try:
        if event_type == "COMMENT":
            return metadata.get("media", {}).get("id")

        if event_type == "STORY_REPLY":
            return metadata.get("reply_to", {}).get("story", {}).get("id")

        attachments = metadata.get("attachments", [])
        for attach in attachments:
            if "id" in attach:
                return attach["id"]

            payload = attach.get("payload", {}) or {}
            
            if "reel_video_id" in payload:
                return payload["reel_video_id"]
            if "story_media_id" in payload:
                return payload["story_media_id"]
            if "id" in payload:
                return payload["id"]
            
            url = payload.get("url")
            if url:
                parsed_url = urlparse(url)
                queries = parse_qs(parsed_url.query)
                asset_ids = queries.get("asset_id")
                if asset_ids:
                    return asset_ids[0]

    except Exception as e:
        logger.error(f"Error parsing fallback media ID context: {e}", exc_info=True)
    return None


@transaction.atomic
def detect_and_create_enquiry(interaction):
    """
    Processes matching products and logs the associated customer CRM Enquiry.
    Uses select_for_update() and atomic transactional blocks to guarantee concurrency protection.
    """
    if interaction.direction != "INBOUND":
        return None

    # Block concurrent threads from evaluating the exact same interaction record simultaneously
    interaction = CustomerInteraction.objects.select_for_update().get(id=interaction.id)

    current_meta = interaction.metadata or {}
    if current_meta.get("crm_processed") is True:
        return None

    # Thread-safe retrieval and lock of the customer to update metrics
    customer = Customer.objects.select_for_update().get(id=interaction.customer.id)
    seller_account = interaction.seller_account
    seller_user = seller_account.user

    if not seller_user:
        logger.warning(f"[CRM MATCH] Unable to process CRM matching. No seller user linked to account {seller_account.id}")
        return None

    # Update customer interaction metrics
    customer.total_interactions += 1
    customer.last_interaction_at = timezone.now()
    customer_update_fields = ["total_interactions", "last_interaction_at"]

    matched_products = []  # List of tuples: (product, confidence_score, matched_media_id)
    media_id = extract_media_id_safely(interaction.event_type, current_meta)
    clean_media_id = str(media_id).strip() if media_id else None

    logger.info(f"[CRM MATCH] Processing interaction {interaction.id}. Extracted media_id: {clean_media_id}")

    # Determine confidence score points based on activity signal intensity
    if interaction.event_type == "DM":
        initial_confidence = 0.5
        score_increment = 0.2
    elif interaction.event_type == "COMMENT":
        initial_confidence = 0.3
        score_increment = 0.1
    else:
        initial_confidence = 0.2
        score_increment = 0.1

    # Matching strictly via direct media IDs querying the product table media_id field
    if clean_media_id:
        products = Product.objects.filter(
            seller=seller_user,
            media_id=clean_media_id,
            status="ACTIVE"
        )
        product_count = products.count()
        logger.info(f"[CRM MATCH] Querying product with media_id='{clean_media_id}'. Found {product_count} database matches.")
        
        for product in products:
            matched_products.append((product, initial_confidence, clean_media_id))
            
        # DIAGNOSTIC ENGINE: Runs if no match was found to check setup issues
        if product_count == 0:
            raw_products = Product.objects.filter(media_id=clean_media_id)
            if raw_products.exists():
                for rp in raw_products:
                    logger.warning(
                        f"[CRM DIAGNOSTIC] Product with media_id='{clean_media_id}' exists in DB but did not match! "
                        f"Checking reasons -> "
                        f"Product Seller: {rp.seller} (Expected: {seller_user}), "
                        f"Product Status: '{rp.status}' (Expected: 'ACTIVE')"
                    )
            else:
                logger.warning(
                    f"[CRM DIAGNOSTIC] Absolutely no product exists in your database with media_id='{clean_media_id}'."
                )
    else:
        logger.info(f"[CRM MATCH] No media ID extracted from interaction {interaction.id}. Skipping product matching.")

    # STRICT GUARD: If no active product matches this interaction, exit early without creating an Enquiry
    if not matched_products:
        logger.info(f"[CRM MATCH] No active product matched for media_id '{clean_media_id}'. Skipping Enquiry creation.")
        
        # Save customer interaction metrics updates (counting the interaction only)
        customer.save(update_fields=customer_update_fields)

        # Flag interaction processing state
        current_meta["crm_processed"] = True
        current_meta["crm_processed_at"] = timezone.now().isoformat()
        interaction.metadata = current_meta
        interaction.save(update_fields=["metadata"])
        return None

    # Build context-specific enquiry filter based on the media ID
    enquiry_filter = {
        "owner": seller_account,
        "customer": customer,
        "media_id": clean_media_id,
        "status__in": ['OPEN', 'ACTIVE']
    }

    # Locking selection to find an existing active enquiry for this customer context
    enquiry = Enquiry.objects.select_for_update().filter(**enquiry_filter).first()

    if not enquiry:
        msg_text = interaction.message_text or ""
        snippet = msg_text[:30] + "..." if len(msg_text) > 30 else msg_text

        first_product = matched_products[0][0]
        title_text = f"[{interaction.event_type}] Interest in {first_product.title or 'Product'} - '{snippet}'"

        try:
            # Wrap in sub-transaction savepoint to safely handle race conditions
            with transaction.atomic():
                enquiry = Enquiry.objects.create(
                    owner=seller_account,
                    customer=customer,
                    source_interaction=interaction,
                    status='OPEN',
                    title=title_text[:255],
                    priority='MEDIUM',
                    media_id=clean_media_id,
                    assigned_to=seller_user
                )
                logger.info(f"Created new open Enquiry {enquiry.id} for Customer {customer.id} on media_id {clean_media_id}.")
                
                # Increment customer's total enquiries metric on brand new creations
                customer.total_enquiries += 1
                customer_update_fields.append("total_enquiries")

        except IntegrityError:
            # Concurrency fallback: retrieve the enquiry created concurrently by the competing thread
            enquiry = Enquiry.objects.select_for_update().filter(**enquiry_filter).first()
            logger.info(f"Concurrent collision handled. Reusing Enquiry {enquiry.id} for Customer {customer.id}.")
    else:
        logger.info(f"Re-using existing active Enquiry {enquiry.id} for Customer {customer.id} with media_id {clean_media_id}.")

    # Save the customer statistics updates
    customer.save(update_fields=customer_update_fields)

    # Save or update matching products to the CRM
    for product, confidence, _ in matched_products:
        # Use select_for_update() to lock and read the most up-to-date score from the database
        enquiry_product = EnquiryProduct.objects.select_for_update().filter(
            enquiry=enquiry,
            product=product
        ).first()

        if enquiry_product:
            current_score = enquiry_product.confidence_score or 0.0
            new_score = min(current_score + score_increment, 1.0)
            enquiry_product.confidence_score = new_score
            enquiry_product.save(update_fields=["confidence_score"])
            
            logger.info(
                f"[CRM MATCH] Increased confidence score for EnquiryProduct {enquiry_product.id} "
                f"from {current_score} by {score_increment} (Activity: {interaction.event_type}) to {enquiry_product.confidence_score}"
            )
        else:
            # If the product is not yet linked, create a new record using the baseline score for this action
            enquiry_product = EnquiryProduct.objects.create(
                enquiry=enquiry,
                product=product,
                is_active=True,
                confidence_score=confidence or initial_confidence
            )
            logger.info(
                f"[CRM MATCH] Created new EnquiryProduct {enquiry_product.id} for Enquiry {enquiry.id} "
                f"and Product {product.id} with initial confidence {enquiry_product.confidence_score}"
            )

    # Flag interaction processing state
    current_meta["crm_processed"] = True
    current_meta["crm_processed_at"] = timezone.now().isoformat()
    interaction.metadata = current_meta
    interaction.save(update_fields=["metadata"])

    return enquiry


def process_interaction_all(interaction):
    """
    Processes a CustomerInteraction through both the CRM Enquiry system 
    and the Visual Automation Workflow engine.
    """
    logger.info(f"[PROCESSOR] Starting full processing for interaction {interaction.id}")
    
    # 1. CRM Enquiry / Product matching
    enquiry = None
    try:
        enquiry = detect_and_create_enquiry(interaction)
    except Exception as e:
        logger.error(f"[PROCESSOR] CRM Enquiry detection failed for interaction {interaction.id}: {e}", exc_info=True)
        
    # 2. Visual Automation Workflow Engine
    try:
        from apps.automations.engine import execute_automation
        execute_automation(interaction)
    except Exception as e:
        logger.error(f"[PROCESSOR] Automation workflow execution failed for interaction {interaction.id}: {e}", exc_info=True)

    return enquiry


@method_decorator(csrf_exempt, name="dispatch")
class InstagramWebhookView(View):

    def check_rate_limit(self, request):
        """
        Application-level caching rate limiter to block endpoint abuse.
        """
        ip = request.META.get('REMOTE_ADDR')
        if not ip:
            return True
        cache_key = f"rl_webhook_{ip}"
        
        try:
            # Atomic evaluation of rate counts (behaves consistently across distributed environments)
            request_count = cache.get(cache_key, 0)
            if request_count > 120:
                return False
            cache.set(cache_key, request_count + 1, timeout=60)
        except Exception as e:
            logger.warning(f"Cache registry failure during rate evaluation: {e}")
        return True

    def get(self, request, *args, **kwargs):
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")

        verify_token = getattr(
            settings,
            "INSTAGRAM_VERIFY_TOKEN",
            getattr(settings, "VERIFY_TOKEN", settings.INSTAGRAM_CLIENT_SECRET)
        )

        if mode == "subscribe" and token == verify_token:
            return HttpResponse(challenge)

        return HttpResponse("Verification failed", status=403)

    def post(self, request, *args, **kwargs):
        # Abort if rate limit exceeded
        if not self.check_rate_limit(request):
            logger.warning(f"Rate limit exceeded on Webhook from IP: {request.META.get('REMOTE_ADDR')}")
            return HttpResponse("Too Many Requests", status=429)

        raw_body = request.body
        signature = request.headers.get("X-Hub-Signature-256", "")

        if not self.verify_signature(raw_body, signature):
            return HttpResponse("Invalid signature", status=403)

        payload = json.loads(raw_body.decode("utf-8"))

        print(payload)

        if payload.get("object") != "instagram":
            return HttpResponse("IGNORED")

        for entry in payload.get("entry", []):
            try:
                owner_id = entry.get("id")

                # Timestamp verification
                entry_time = entry.get("time")
                if entry_time:
                    try:
                        ts = float(entry_time) / 1000.0 if len(str(int(entry_time))) > 10 else float(entry_time)
                        current_ts = timezone.now().timestamp()
                        
                        if ts < current_ts - 3600 or ts > current_ts + 300:
                            logger.warning(f"Rejected payload entry due to timestamp boundaries: {ts}")
                            continue
                    except Exception as e:
                        logger.error(f"Error checking entry age parameters: {e}")

                owner_account = InstagramAccount.objects.filter(
                    Q(instagram_scoped_id=owner_id) |
                    Q(instagram_user_id=owner_id)
                ).first()

                if not owner_account:
                    continue

                # =========================================================
                # MESSAGING (DM / REACTIONS / POSTBACK / READ)
                # =========================================================
                for event in entry.get("messaging", []):
                    try:
                        sender_id = event.get("sender", {}).get("id")
                        recipient_id = event.get("recipient", {}).get("id")
                        timestamp_ms = event.get("timestamp")

                        platform_timestamp = None
                        if timestamp_ms:
                            try:
                                platform_timestamp = datetime.datetime.fromtimestamp(
                                    timestamp_ms / 1000.0,
                                    tz=datetime.timezone.utc
                                )
                            except Exception:
                                pass

                        if str(sender_id) == str(owner_id):
                            direction = "OUTBOUND"
                            customer_id = recipient_id
                        else:
                            direction = "INBOUND"
                            customer_id = sender_id

                        if not customer_id:
                            continue

                        customer, created = Customer.objects.get_or_create(
                            owner=owner_account,
                            instagram_scoped_id=customer_id
                        )
                        if created or not customer.username and not customer.full_name:
                            sync_customer_profile_task.delay(customer.id)
                        # -------------------------
                        # MESSAGE
                        # -------------------------
                        if "message" in event:
                            msg_data = event["message"]
                            mid = msg_data.get("mid")

                            if mid and CustomerInteraction.objects.filter(
                                instagram_event_id=mid
                            ).exists():
                                continue

                            text_content = msg_data.get("text", "")[:1000]
                            attachments = msg_data.get("attachments", [])

                            message_type = "TEXT"
                            media_url = None
                            media_id = None

                            reply_to = msg_data.get("reply_to")
                            if reply_to and isinstance(reply_to, dict) and "story" in reply_to:
                                story_info = reply_to["story"]
                                if isinstance(story_info, dict):
                                    media_id = story_info.get("id")

                            if attachments:
                                attachment = attachments[0]
                                attachment_type = attachment.get("type")
                                payload = attachment.get("payload", {})

                                if attachment_type == "image":
                                    message_type = "IMAGE"

                                elif attachment_type == "video":
                                    message_type = "VIDEO"

                                elif attachment_type == "audio":
                                    message_type = "AUDIO"

                                elif attachment_type == "file":
                                    message_type = "FILE"

                                elif attachment_type == "ig_post":
                                    media_id = payload.get("ig_post_media_id")

                                    # If Instagram later exposes carousel info
                                    title = (payload.get("title") or "").lower()
                                    if "carousel" in title:
                                        message_type = "CAROUSEL"
                                    else:
                                        message_type = "POST"

                                elif attachment_type == "ig_reel":
                                    message_type = "REEL"
                                    media_id = payload.get("reel_video_id")

                                media_url = payload.get("url")

                            interaction = CustomerInteraction.objects.create(
                                customer=customer,
                                seller_account=owner_account,
                                event_type="DM",
                                direction=direction,
                                message_type=message_type,
                                message_text=text_content,
                                media_url=media_url,
                                media_id=media_id,
                                instagram_event_id=mid,
                                platform_timestamp=platform_timestamp,
                                metadata={
                                    "crm_processed": False,
                                    "attachments": attachments,
                                    "reply_to": msg_data.get("reply_to"),
                                    "is_echo": msg_data.get("is_echo", False),
                                }
                            )

                            # Run automation engine synchronously for instant replies
                            try:
                                from apps.automations.engine import execute_automation
                                execute_automation(interaction)
                            except Exception as auto_err:
                                logger.error(f"Error running automation synchronously: {auto_err}", exc_info=True)

                            # Trigger processing (tries asynchronous execution, falls back to synchronous execution)
                            if CELERY_AVAILABLE:
                                try:
                                    process_enquiry_background_task.delay(interaction.id)
                                except Exception as e:
                                    logger.error(
                                        f"Celery delivery failure for ID {interaction.id}. "
                                        f"Attempting synchronous fallback matching. Error: {e}"
                                    )
                                    try:
                                        process_interaction_all(interaction)
                                    except Exception as sync_err:
                                        logger.error(f"Synchronous fallback failed for interaction {interaction.id}: {sync_err}", exc_info=True)
                            else:
                                logger.warning(f"Celery task is offline. Executing synchronous matching fallback.")
                                try:
                                    process_interaction_all(interaction)
                                except Exception as sync_err:
                                    logger.error(f"Synchronous execution failed for interaction {interaction.id}: {sync_err}", exc_info=True)

                        # -------------------------
                        # REACTION
                        # -------------------------
                        elif "reaction" in event:
                            reaction = event["reaction"]
                            target_mid = reaction.get("mid")

                            target_msg = CustomerInteraction.objects.filter(
                                instagram_event_id=target_mid
                            ).first()

                            if target_msg:
                                meta = target_msg.metadata or {}
                                reactions_history = meta.get("reactions_history", [])

                                timestamp_str = (
                                    platform_timestamp.isoformat()
                                    if platform_timestamp else timezone.now().isoformat()
                                )

                                if reaction.get("action") == "react":
                                    reactions_history.append({
                                        "action": "react",
                                        "emoji": reaction.get("emoji"),
                                        "reaction": reaction.get("reaction"),
                                        "customer_id": sender_id,
                                        "timestamp": timestamp_str
                                    })
                                elif reaction.get("action") == "unreact":
                                    reactions_history.append({
                                        "action": "unreact",
                                        "emoji": reaction.get("emoji"),
                                        "customer_id": sender_id,
                                        "timestamp": timestamp_str
                                    })

                                # Restrict array length to prevent payload bloat inside JSON fields
                                meta["reactions_history"] = reactions_history[-50:]
                                target_msg.metadata = meta
                                target_msg.save(update_fields=["metadata"])

                        # -------------------------
                        # POSTBACK
                        # -------------------------
                        elif "postback" in event:
                            postback = event["postback"]
                            mid = postback.get("mid")

                            interaction = CustomerInteraction.objects.create(
                                customer=customer,
                                seller_account=owner_account,
                                event_type="CLICK",
                                direction=direction,
                                message_text=f"Postback: {postback.get('payload')}"[:1000],
                                instagram_event_id=mid,
                                platform_timestamp=platform_timestamp,
                                metadata={"crm_processed": False, "postback": postback}
                            )

                            # Run automation engine synchronously for instant replies
                            try:
                                from apps.automations.engine import execute_automation
                                execute_automation(interaction)
                            except Exception as auto_err:
                                logger.error(f"Error running automation synchronously: {auto_err}", exc_info=True)

                            if CELERY_AVAILABLE:
                                try:
                                    process_enquiry_background_task.delay(interaction.id)
                                except Exception as e:
                                    logger.error(
                                        f"Celery task delivery failed for Postback Interaction {interaction.id}. "
                                        f"Attempting synchronous matching fallback. Error: {e}"
                                    )
                                    try:
                                        process_interaction_all(interaction)
                                    except Exception as sync_err:
                                        logger.error(f"Synchronous fallback failed for Postback: {sync_err}", exc_info=True)
                            else:
                                logger.warning(f"Celery task is offline. Processing postback synchronously.")
                                try:
                                    process_interaction_all(interaction)
                                except Exception as sync_err:
                                    logger.error(f"Synchronous execution failed for Postback: {sync_err}", exc_info=True)

                        # -------------------------
                        # READ
                        # -------------------------
                        elif "read" in event:
                            read = event["read"]
                            CustomerInteraction.objects.filter(
                                instagram_event_id=read.get("mid")
                            ).update(is_read=True)

                        

                    except Exception as event_err:
                        logger.error(f"Error handling event payload: {event_err}", exc_info=True)
                        continue

                # =========================================================
                # COMMENTS (FEED / REELS)
                # =========================================================
                for change in entry.get("changes", []):
                    try:
                        if change.get("field") != "comments":
                            continue

                        value = change.get("value", {})
                        from_user = value.get("from", {})

                        from_id = from_user.get("id")
                        username = from_user.get("username")
                        text = value.get("text")
                        comment_id = value.get("id")
                        media_info = value.get("media", {})
                        parent_id = value.get("parent_id")
                        
                        # Comment idempotency check
                        if comment_id and CustomerInteraction.objects.filter(instagram_event_id=comment_id).exists():
                            continue

                        change_time = entry.get("time")
                        platform_timestamp = None

                        if change_time:
                            try:
                                ts = float(change_time)
                                if len(str(int(ts))) > 10:
                                    ts = ts / 1000.0

                                platform_timestamp = datetime.datetime.fromtimestamp(
                                    ts,
                                    tz=datetime.timezone.utc
                                )
                            except Exception:
                                pass

                        if str(from_id) == str(owner_id):
                            direction = "OUTBOUND"
                        else:
                            direction = "INBOUND"

                        customer = None
                        if from_id:
                            customer,created = Customer.objects.get_or_create(
                                owner=owner_account,
                                instagram_scoped_id=from_id
                            )

                        if created or not customer.username and not customer.full_name:
                            sync_customer_profile_task.delay(customer.id)

                        if customer:
                            interaction = CustomerInteraction.objects.create(
                                customer=customer,
                                seller_account=owner_account,
                                event_type="COMMENT",
                                media_id=media_info.get("id"),
                                direction=direction,
                                message_text=text[:1000] if text else "",
                                instagram_event_id=comment_id,
                                platform_timestamp=platform_timestamp,
                                metadata={
                                    "crm_processed": False,
                                    "media": media_info,
                                    "username": username,
                                    "parent_comment_id": parent_id
                                }
                            )

                            # Run automation engine synchronously for instant replies
                            try:
                                from apps.automations.engine import execute_automation
                                execute_automation(interaction)
                            except Exception as auto_err:
                                logger.error(f"Error running automation synchronously: {auto_err}", exc_info=True)

                            if CELERY_AVAILABLE:
                                try:
                                    process_enquiry_background_task.delay(interaction.id)
                                except Exception as e:
                                    logger.error(
                                        f"Celery task delivery failed for Comment Interaction {interaction.id}. "
                                        f"Attempting synchronous fallback matching. Error: {e}"
                                    )
                                    try:
                                        process_interaction_all(interaction)
                                    except Exception as sync_err:
                                        logger.error(f"Synchronous fallback failed for Comment: {sync_err}", exc_info=True)
                            else:
                                logger.warning(f"Celery task is offline. Processing Comment synchronously.")
                                try:
                                    process_interaction_all(interaction)
                                except Exception as sync_err:
                                    logger.error(f"Synchronous execution failed for Comment: {sync_err}", exc_info=True)

                    except Exception as change_err:
                        logger.error(f"Error handling comments/changes: {change_err}", exc_info=True)
                        continue

            except Exception as entry_err:
                logger.error(f"Error handling webhook entry block: {entry_err}", exc_info=True)
                continue

        return HttpResponse("EVENT_RECEIVED")

    def verify_signature(self, payload_body, signature_header):
        if not signature_header or not signature_header.startswith("sha256="):
            return False

        secret = settings.INSTAGRAM_CLIENT_SECRET
        if isinstance(secret, str):
            secret = secret.encode("utf-8")

        expected = hmac.new(
            secret,
            payload_body,
            hashlib.sha256
        ).hexdigest()

        incoming = signature_header[7:]

        return hmac.compare_digest(expected, incoming)

import requests
import threading

def fetch_and_save_profile_pic_background(customer_id, access_token):
    def task():
        try:
            from apps.crm.models import Customer
            customer = Customer.objects.get(id=customer_id)
            
            needs_update = False
            if not customer.profile_pic:
                needs_update = True
            else:
                try:
                    # Send a quick HEAD request to verify URL validity
                    r = requests.head(customer.profile_pic, timeout=5)
                    if r.status_code != 200:
                        needs_update = True
                except Exception:
                    needs_update = True
            
            if needs_update:
                url = f"https://graph.instagram.com/v25.0/{customer.instagram_scoped_id}"
                params = {
                    "fields": "profile_pic,username",
                    "access_token": access_token
                }
                res = requests.get(url, params=params, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    profile_pic = data.get("profile_pic")
                    if profile_pic:
                        customer.profile_pic = profile_pic
                        customer.save(update_fields=["profile_pic"])
                        logger.info(f"Successfully updated profile picture for customer {customer.username}")
        except Exception as e:
            logger.error(f"Error fetching profile pic in background: {e}")

    threading.Thread(target=task).start()


from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

class InstagramConversationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        user = request.user
        try:
            user.refresh_from_db(fields=['active_instagram_account'])
        except Exception:
            pass
        account = user.active_instagram_account
        if not account:
            account = InstagramAccount.objects.filter(user=user, is_active=True).first()
            
        if not account:
            return Response({'error': 'No active Instagram account found'}, status=400)
            
        ig_user_id = account.instagram_user_id
        access_token = account.access_token

        if not ig_user_id or not access_token:
            return Response({'error': 'Instagram account details incomplete'}, status=400)
            
        url = f"https://graph.instagram.com/v25.0/me/conversations?platform=instagram&fields=participants,updated_time,messages.limit(1){{message}}"
        headers = {
            'Authorization': f'Bearer {access_token}'
        }
        
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            conversations = data.get('data', [])
            
            # Enrich each conversation with 24-hour window status
            from django.utils import timezone
            from datetime import timedelta
            from apps.crm.models import Customer, CustomerInteraction
            
            cutoff = timezone.now() - timedelta(hours=24)
            customers = {c.instagram_scoped_id: c for c in Customer.objects.filter(owner=account)}
            
            for conv in conversations:
                participants = conv.get('participants', {}).get('data', [])
                recipient_id = None
                for p in participants:
                    if str(p.get('id')) != str(ig_user_id):
                        recipient_id = p.get('id')
                        break
                
                conv['is_within_24h_window'] = False
                conv['profile_pic'] = None
                if recipient_id:
                    customer = customers.get(recipient_id)
                    if customer:
                        has_recent_inbound = CustomerInteraction.objects.filter(
                            customer=customer,
                            direction="INBOUND",
                            platform_timestamp__gte=cutoff
                        ).exists()
                        conv['is_within_24h_window'] = has_recent_inbound
                        conv['profile_pic'] = customer.profile_pic
                        fetch_and_save_profile_pic_background(customer.id, access_token)
            
            return Response({
                'conversations': conversations,
                'business_username': account.username,
                'business_id': ig_user_id
            })
        except requests.RequestException as e:
            logger.error(f"Error fetching Instagram conversations: {e}")
            err_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                err_msg = e.response.text
            return Response({'error': err_msg}, status=500)


class InstagramConversationMessagesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id, *args, **kwargs):
        user = request.user
        try:
            user.refresh_from_db(fields=['active_instagram_account'])
        except Exception:
            pass
        account = user.active_instagram_account
        if not account:
            account = InstagramAccount.objects.filter(user=user, is_active=True).first()
            
        if not account:
            return Response({'error': 'No active Instagram account found'}, status=400)
            
        access_token = account.access_token
        if not access_token:
            return Response({'error': 'Instagram account details incomplete'}, status=400)
            
        url = f"https://graph.instagram.com/v25.0/{conversation_id}"
        
        # Get optional 'after' cursor from query params for pagination
        after_cursor = request.query_params.get('after')
        
        params = {
            "fields": "participants,messages{id,message,created_time,from,to,attachments,shares,story}",
            "access_token": access_token
        }
        if after_cursor:
            params["fields"] = f"participants,messages.after({after_cursor}){{id,message,created_time,from,to,attachments,shares,story}}"
        
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            messages_conn = data.get('messages', {})
            
            # Check 24-hour window status
            participants = data.get('participants', {}).get('data', [])
            recipient_id = None
            for p in participants:
                if str(p.get('id')) != str(account.instagram_user_id):
                    recipient_id = p.get('id')
                    break

            is_within_24h_window = False
            last_interaction_time = None
            
            if recipient_id:
                from django.utils import timezone
                from datetime import timedelta
                from apps.crm.models import Customer, CustomerInteraction
                
                customer = Customer.objects.filter(
                    instagram_scoped_id=recipient_id,
                    owner=account
                ).first()
                
                if customer:
                    cutoff = timezone.now() - timedelta(hours=24)
                    last_inbound = CustomerInteraction.objects.filter(
                        customer=customer,
                        direction="INBOUND"
                    ).order_by('-platform_timestamp').first()
                    
                    if last_inbound:
                        last_interaction_time = last_inbound.platform_timestamp.isoformat()
                        if last_inbound.platform_timestamp >= cutoff:
                            is_within_24h_window = True
                    
                    fetch_and_save_profile_pic_background(customer.id, access_token)
            
            return Response({
                'messages': messages_conn.get('data', []),
                'next_cursor': messages_conn.get('paging', {}).get('cursors', {}).get('after'),
                'is_within_24h_window': is_within_24h_window,
                'last_interaction_time': last_interaction_time
            })
        except requests.RequestException as e:
            logger.error(f"Error fetching Instagram messages for conversation {conversation_id}: {e}")
            err_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                err_msg = e.response.text
            return Response({'error': err_msg}, status=500)


class CustomerEnquiriesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        username = request.query_params.get("username")
        if not username:
            return Response({"error": "username parameter is required"}, status=400)

        user = request.user
        active_account = user.active_instagram_account
        if not active_account:
            active_account = user.instagram_accounts.filter(is_active=True).first()

        if not active_account:
            return Response({"error": "No active Instagram account found"}, status=400)

        # Find the customer
        customer = Customer.objects.filter(
            username__iexact=username,
            owner=active_account
        ).first()

        if not customer:
            return Response({"enquiries": []})

        # Fetch enquiries
        enquiries = Enquiry.objects.filter(
            customer=customer,
            owner=active_account
        ).prefetch_related('products__product').order_by('-created_at')

        data = []
        for eq in enquiries:
            products_data = []
            for ep in eq.products.all():
                products_data.append({
                    "enquiry_product_id": ep.id,
                    "product_id": ep.product.id,
                    "title": ep.product.title or "Untitled Product",
                    "price": str(ep.product.price) if ep.product.price else None,
                    "main_media_url": ep.product.main_media_url,
                    "confidence_score": ep.confidence_score,
                })
            
            data.append({
                "id": eq.id,
                "title": eq.title,
                "status": eq.status,
                "priority": eq.priority,
                "media_id": eq.media_id,
                "created_at": eq.created_at.isoformat(),
                "products": products_data
            })

        return Response({"enquiries": data})


class DeleteEnquiryProductView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            user = request.user
            active_account = user.active_instagram_account
            if not active_account:
                active_account = user.instagram_accounts.filter(is_active=True).first()

            if not active_account:
                return Response({"error": "No active Instagram account found"}, status=400)

            # Ensure the enquiry product belongs to the user's active account
            enquiry_product = EnquiryProduct.objects.get(
                id=pk,
                enquiry__owner=active_account
            )
            
            enquiry = enquiry_product.enquiry
            enquiry_product.delete()
            
            # If no products left in this enquiry, delete the enquiry as well
            if not enquiry.products.exists():
                enquiry.delete()
                
            return Response({"message": "Product removed from enquiry successfully"}, status=200)
        except EnquiryProduct.DoesNotExist:
            return Response({"error": "Enquiry product not found"}, status=404)


class SendInstagramMessageView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        user = request.user
        active_account = user.active_instagram_account
        if not active_account:
            active_account = user.instagram_accounts.filter(is_active=True).first()

        if not active_account or not active_account.access_token:
            return Response({"error": "No active Instagram account connected"}, status=400)

        recipient_id = request.data.get("recipient_id")
        if not recipient_id:
            # Fallback: try to extract from conversation_id (format: t_id1_id2)
            parts = conversation_id.split("_")
            if len(parts) >= 3 and parts[0] == "t":
                bus_id = active_account.instagram_user_id or active_account.instagram_scoped_id
                for p in parts[1:]:
                    if p != bus_id:
                        recipient_id = p
                        break

        if not recipient_id:
            return Response({"error": "recipient_id is required"}, status=400)

        # Enforce 24-hour messaging window
        from django.utils import timezone
        from datetime import timedelta
        from apps.crm.models import Customer, CustomerInteraction

        customer = Customer.objects.filter(
            instagram_scoped_id=recipient_id,
            owner=active_account
        ).first()

        if customer:
            cutoff = timezone.now() - timedelta(hours=24)
            has_recent_inbound = CustomerInteraction.objects.filter(
                customer=customer,
                direction="INBOUND",
                platform_timestamp__gte=cutoff
            ).exists()

            if not has_recent_inbound:
                return Response({
                    "error": "Out of 24-hour messaging window. You can only send messages to users who have interacted with you in the last 24 hours."
                }, status=403)
        else:
            # Fallback check: if we have NO customer record, we have no recorded inbound interaction in 24 hours
            return Response({
                "error": "No recent interaction found. You can only send messages to users who have interacted with you in the last 24 hours."
            }, status=403)

        message_payload = request.data.get("message")
        if not message_payload:
            return Response({"error": "message payload is required"}, status=400)

        is_basic = active_account.access_token.startswith("IGAA")
        host = "graph.instagram.com" if is_basic else "graph.facebook.com"
        url = f"https://{host}/v25.0/me/messages"

        headers = {
            "Authorization": f"Bearer {active_account.access_token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "recipient": {"id": recipient_id},
            "message": message_payload
        }

        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            r.raise_for_status()
            response_data = r.json()
            
            # Log the outbound interaction in our DB
            try:
                customer = Customer.objects.filter(
                    instagram_scoped_id=recipient_id,
                    owner=active_account
                ).first()
                if customer:
                    text_content = message_payload.get("text", "")
                    msg_type = "TEXT"
                    
                    if "attachment" in message_payload:
                        att = message_payload["attachment"]
                        if att.get("type") == "template":
                            tpl = att.get("payload", {})
                            if tpl.get("template_type") == "button":
                                msg_type = "BUTTON_TEMPLATE"
                                text_content = tpl.get("text", "")
                            elif tpl.get("template_type") == "generic":
                                msg_type = "GENERIC_TEMPLATE"
                                elems = tpl.get("elements", [])
                                if elems:
                                    text_content = elems[0].get("title", "")
                    
                    CustomerInteraction.objects.create(
                        customer=customer,
                        seller_account=active_account,
                        event_type="DM",
                        direction="OUTBOUND",
                        message_type=msg_type,
                        message_text=text_content,
                        instagram_event_id=response_data.get("message_id"),
                        platform_timestamp=timezone.now(),
                        metadata={"sent_payload": payload}
                    )
            except Exception as db_err:
                logger.error(f"Error logging outbound interaction: {db_err}")

            return Response(response_data)
        except requests.RequestException as e:
            logger.error(f"Error sending Instagram message: {e}")
            err_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                err_msg = e.response.text
            return Response({"error": err_msg}, status=500)


class UploadImageView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import os
        from django.conf import settings
        from django.core.files.storage import default_storage
        from django.core.files.base import ContentFile

        file_obj = request.FILES.get('image')
        if not file_obj:
            return Response({"error": "No image file provided"}, status=400)
        
        path = default_storage.save(os.path.join('uploads', file_obj.name), ContentFile(file_obj.read()))
        file_url = request.build_absolute_uri(settings.MEDIA_URL + path)
        
        return Response({"url": file_url})