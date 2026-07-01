import requests
import json
import logging
import random
import hashlib
from django.utils import timezone
from django.db.models import Q
from apps.automations.models import AutomationRule, AutomationAction, AutomationExecution

logger = logging.getLogger(__name__)

def resolve_dynamic_prices(message_data, dm_format):
    """
    Looks for {{price}} and {{name}} in button titles and dynamically replaces them
    with the actual price and title of the product from the database.
    """
    from apps.products.models import Product

    def get_product_details_for_url(url):
        if not url or "/product/" not in url:
            return None, None
        try:
            # Extract product ID after '/product/'
            parts = url.split("/product/")
            if len(parts) > 1:
                prod_id = parts[1].split("?")[0].split("/")[0].strip()
                product = Product.objects.filter(id=prod_id).first()
                if product:
                    price_str = None
                    if product.price is not None:
                        price_val = float(product.price)
                        formatted_price = f"{int(price_val)}" if price_val.is_integer() else f"{price_val:.2f}"
                        price_str = f"₹{formatted_price}"
                    return price_str, product.title
        except Exception as e:
            logger.error(f"[ENGINE] Error resolving dynamic details for URL {url}: {e}", exc_info=True)
        return None, None

    def process_button(btn):
        title = btn.get("title", "")
        if "{{price}}" in title or "{{name}}" in title:
            price_str, prod_title = get_product_details_for_url(btn.get("url", ""))
            if price_str and "{{price}}" in title:
                title = title.replace("{{price}}", price_str)
            if prod_title and "{{name}}" in title:
                title = title.replace("{{name}}", prod_title)
            btn["title"] = title[:20]

    if dm_format == "button_template":
        buttons = message_data.get("buttons", [])
        for btn in buttons:
            process_button(btn)

    elif dm_format == "generic_template":
        elements = message_data.get("elements", [])
        for elem in elements:
            # Resolve placeholders in Card Title and Subtitle using default_action.url
            url = elem.get("default_action", {}).get("url", "") if elem.get("default_action") else ""
            price_str, prod_title = get_product_details_for_url(url)
            
            title = elem.get("title", "")
            if "{{price}}" in title or "{{name}}" in title:
                if price_str and "{{price}}" in title:
                    title = title.replace("{{price}}", price_str)
                if prod_title and "{{name}}" in title:
                    title = title.replace("{{name}}", prod_title)
                elem["title"] = title[:80]
                
            subtitle = elem.get("subtitle", "")
            if "{{price}}" in subtitle or "{{name}}" in subtitle:
                if price_str and "{{price}}" in subtitle:
                    subtitle = subtitle.replace("{{price}}", price_str)
                if prod_title and "{{name}}" in subtitle:
                    subtitle = subtitle.replace("{{name}}", prod_title)
                elem["subtitle"] = subtitle[:80]

            buttons = elem.get("buttons", [])
            for btn in buttons:
                process_button(btn)


def send_instagram_dm(account, recipient_id, message_data, dm_format="text", recipient_type="id"):
    """
    Sends a direct message to a user using the Meta Instagram Messaging API v25.0.
    Supports text, quick replies, button templates, and generic (carousel) templates.
    """
    access_token = account.access_token
    instagram_scoped_id = account.instagram_scoped_id or account.instagram_user_id

    if not access_token or not instagram_scoped_id:
        logger.error(f"Cannot send DM: Account {account.id} missing access token or Instagram scoped ID.")
        return False, "Missing credentials"

    url = f"https://graph.instagram.com/v25.0/{instagram_scoped_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Resolve any dynamic {{price}} placeholders in button titles
    try:
        resolve_dynamic_prices(message_data, dm_format)
    except Exception as e:
        logger.error(f"[ENGINE] Failed to resolve dynamic prices: {e}", exc_info=True)

    # Build the payload based on dm_format
    message_payload = {}
    if dm_format == "text":
        message_payload = {"text": message_data.get("text", "")}
    elif dm_format == "quick_reply":
        message_payload = {
            "text": message_data.get("text", ""),
            "quick_replies": message_data.get("quick_replies", [])
        }
    elif dm_format == "button_template":
        message_payload = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": message_data.get("text", ""),
                    "buttons": message_data.get("buttons", [])
                }
            }
        }
    elif dm_format == "generic_template":
        message_payload = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": message_data.get("elements", [])
                }
            }
        }
    elif dm_format == "attachment":
        att_type = message_data.get("attachment_type", "image")
        if att_type == "images":
            message_payload = {
                "attachments": [
                    {"type": "image", "payload": {"url": u}}
                    for u in message_data.get("urls", []) if u
                ]
            }
        elif att_type == "sticker":
            sticker_val = message_data.get("sticker_id", "like_heart")
            if str(sticker_val).isdigit():
                message_payload = {
                    "sticker_id": int(sticker_val)
                }
            else:
                message_payload = {
                    "attachment": {
                        "type": sticker_val or "like_heart"
                    }
                }
        elif att_type == "MEDIA_SHARE":
            message_payload = {
                "attachment": {
                    "type": "MEDIA_SHARE",
                    "payload": {
                        "id": str(message_data.get("media_id", ""))
                    }
                }
            }
        else:
            message_payload = {
                "attachment": {
                    "type": att_type,
                    "payload": {
                        "url": message_data.get("url", "")
                    }
                }
            }
    else:
        # Fallback to plain text
        message_payload = {"text": message_data.get("text", str(message_data))}

    payload = {
        "recipient": {recipient_type: str(recipient_id)},
        "message": message_payload
    }

    try:
        # Print a sample curl command for easy terminal debugging
        import json
        curl_payload = json.dumps(payload, ensure_ascii=False)
        try:
            print(f"\n[DEBUG CURL CALL]:\ncurl -X POST \"{url}\" \\\n  -H \"Authorization: Bearer {access_token}\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{curl_payload}'\n")
        except Exception:
            try:
                print(f"\n[DEBUG CURL CALL]:\ncurl -X POST \"{url}\" ... (payload printed with ascii replacement)")
            except Exception:
                pass
        
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response_data = response.json()
        if response.status_code == 200:
            print(f"[ENGINE] DM successfully sent to {recipient_id}: {response_data.get('message_id')}")
            logger.info(f"DM successfully sent to {recipient_id}: {response_data.get('message_id')}")
            return True, response_data
        else:
            print(f"[ENGINE] Failed to send DM to {recipient_id}: Status {response.status_code}, Response: {response_data}")
            logger.error(f"Failed to send DM to {recipient_id}: Status {response.status_code}, Response: {response_data}")
            return False, response_data.get("error", {}).get("message", "API Error")
    except Exception as e:
        print(f"[ENGINE] Exception sending DM to {recipient_id}: {e}")
        logger.error(f"Exception sending DM to {recipient_id}: {e}", exc_info=True)
        return False, str(e)


def reply_instagram_comment(account, comment_id, message_text):
    """
    Replies to a comment on a post using the Meta Instagram Comment Moderation API v25.0.
    """
    access_token = account.access_token
    if not access_token:
        logger.error(f"Cannot reply to comment: Account {account.id} missing access token.")
        return False, "Missing credentials"

    url = f"https://graph.instagram.com/v25.0/{comment_id}/replies"
    params = {"access_token": access_token}
    data = {"message": message_text}

    try:
        response = requests.post(url, params=params, json=data, timeout=15)
        response_data = response.json() 
        if response.status_code == 200:
            logger.info(f"Comment reply successfully sent for comment {comment_id}: {response_data.get('id')}")
            return True, response_data
        else:
            logger.error(f"Failed to reply to comment {comment_id}: Status {response.status_code}, Response: {response_data}")
            return False, response_data.get("error", {}).get("message", "API Error")
    except Exception as e:
        logger.error(f"Exception replying to comment {comment_id}: {e}", exc_info=True)
        return False, str(e)
def execute_automation(interaction):
    """
    Processes an inbound customer interaction, matches it to active automation rules,
    and executes the corresponding actions. Supports multi-level flow graph traversal.
    """
    print(f"[ENGINE] execute_automation called. ID={interaction.id}, Direction={interaction.direction}, Event={interaction.event_type}, Text='{interaction.message_text}'")
    if interaction.direction != "INBOUND":
        print(f"[ENGINE] Ignored: Direction is not INBOUND ({interaction.direction})")
        return

    seller_account = interaction.seller_account
    customer = interaction.customer
    message_text = (interaction.message_text or "").strip()
    event_type = interaction.event_type  # DM, COMMENT, CLICK (postback)
    media_id = interaction.media_id

    print(f"[ENGINE] Processing interaction {interaction.id} from customer {customer.id} (username: {customer.username}). Event: {event_type}, Msg: '{message_text}', Media: '{media_id}'")
    logger.info(f"[ENGINE] Processing interaction {interaction.id} from customer {customer.id} (username: {customer.username}). Event: {event_type}, Msg: '{message_text}', Media: '{media_id}'")

    # ─────────────────────────────────────────────────────────────────────────
    # HANDLE MULTI-LEVEL FLOW BUTTON/QUICK-REPLY CLICKS (POSTBACK)
    # ─────────────────────────────────────────────────────────────────────────
    payload_str = ""
    if event_type == "CLICK":
        payload_str = (interaction.metadata or {}).get("postback", {}).get("payload", "")
        if not payload_str:
            payload_str = (interaction.metadata or {}).get("quick_reply", {}).get("payload", "")
        if not payload_str and message_text.startswith("Postback: "):
            payload_str = message_text.replace("Postback: ", "")

    if event_type == "CLICK" and payload_str:
        print(f"[ENGINE] Handling postback click with payload: {payload_str}")
        logger.info(f"[ENGINE] Handling postback click with payload: {payload_str}")
        
        # Backward compatibility for quick reply payloads (QR_PAYLOAD_XS vs QR_XS)
        payload_list = [payload_str]
        if payload_str.startswith("QR_PAYLOAD_"):
            payload_list.append(payload_str.replace("QR_PAYLOAD_", "QR_"))
        elif payload_str.startswith("QR_"):
            payload_list.append(payload_str.replace("QR_", "QR_PAYLOAD_"))

        # Find active actions matching this parent_event
        matching_actions = AutomationAction.objects.filter(
            parent_event__in=payload_list,
            rule__seller=seller_account,
            rule__status='active'
        ).select_related('rule')
        
        if matching_actions.exists():
            print(f"[ENGINE] Found {matching_actions.count()} matching actions for postback {payload_str}.")
            for action in matching_actions:
                rule = action.rule
                actions_log = []
                action_type = action.action_type
                dm_format = action.dm_format or "text"
                
                selected_msg = ""
                if action.messages:
                    if action.message_mode == "random":
                        selected_msg = random.choice(action.messages)
                    elif action.message_mode == "fixed" or not action.message_mode:
                        selected_msg = action.messages[0]
                    elif action.message_mode == "sequential":
                        exec_count = AutomationExecution.objects.filter(rule=rule).count()
                        selected_msg = action.messages[exec_count % len(action.messages)]
                
                action_success = False
                error_details = None

                if action_type == "reply_comment":
                    if interaction.instagram_event_id:
                        action_success, resp = reply_instagram_comment(
                            seller_account,
                            interaction.instagram_event_id,
                            selected_msg
                        )
                        if not action_success:
                            error_details = resp
                    else:
                        error_details = "Cannot reply to comment: Missing comment ID."

                elif action_type in ["send_dm", "reply_story"]:
                    recipient_id = customer.instagram_scoped_id
                    if dm_format == "attachment":
                        attachments = action.attachment_payload or []
                        if not attachments:
                            action_success = True
                        else:
                            action_success = True
                            error_details_list = []
                            
                            # Group consecutive images together
                            grouped_runs = []
                            current_image_group = []
                            
                            for att in attachments:
                                att_type = att.get("type", "image")
                                if att_type == "image":
                                    current_image_group.append(att)
                                else:
                                    if current_image_group:
                                        grouped_runs.append(("image_group", current_image_group))
                                        current_image_group = []
                                    grouped_runs.append((att_type, att))
                            if current_image_group:
                                grouped_runs.append(("image_group", current_image_group))
                                
                            for run_type, run_data in grouped_runs:
                                if run_type == "image_group":
                                    if len(run_data) > 1:
                                        msg_data = {
                                            "attachment_type": "images",
                                            "urls": [item.get("url", "") for item in run_data]
                                        }
                                    else:
                                        msg_data = {
                                            "attachment_type": "image",
                                            "url": run_data[0].get("url", "")
                                        }
                                elif run_type == "sticker":
                                    msg_data = {
                                        "attachment_type": "sticker",
                                        "sticker_id": run_data.get("sticker_id", "like_heart")
                                    }
                                elif run_type == "MEDIA_SHARE":
                                    msg_data = {
                                        "attachment_type": "MEDIA_SHARE",
                                        "media_id": run_data.get("media_id")
                                    }
                                else:
                                    msg_data = {
                                        "attachment_type": run_type,
                                        "url": run_data.get("url", "")
                                    }
                                    
                                success, resp = send_instagram_dm(
                                    seller_account,
                                    recipient_id,
                                    msg_data,
                                    dm_format=dm_format,
                                    recipient_type="id"
                                )
                                if not success:
                                    action_success = False
                                    error_details_list.append(str(resp))
                            if not action_success:
                                error_details = "; ".join(error_details_list)
                    else:
                        msg_data = {"text": selected_msg}
                        if dm_format == "quick_reply":
                            msg_data["quick_replies"] = action.quick_reply_payload.get("quick_replies", [])
                        elif dm_format == "button_template":
                            msg_data["buttons"] = action.button_template_payload.get("buttons", [])
                        elif dm_format == "generic_template":
                            msg_data["elements"] = action.generic_template_payload.get("elements", [])

                        action_success, resp = send_instagram_dm(
                            seller_account,
                            recipient_id,
                            msg_data,
                            dm_format=dm_format,
                            recipient_type="id"
                        )
                        if not action_success:
                            error_details = resp

                # Log execution specifically for this action
                event_hash_input = f"{rule.id}:{interaction.instagram_event_id or interaction.id}:{action.id}"
                event_hash = hashlib.sha256(event_hash_input.encode('utf-8')).hexdigest()

                AutomationExecution.objects.create(
                    rule=rule,
                    customer=customer,
                    trigger_event_type=event_type,
                    trigger_text=message_text,
                    trigger_media_id=media_id,
                    trigger_event_hash=event_hash,
                    status="success" if action_success else "failed",
                    actions_log=[{
                        "action_type": action_type,
                        "dm_format": dm_format,
                        "status": "success" if action_success else "failed",
                        "message_sent": selected_msg,
                        "error": str(error_details) if error_details else None
                    }],
                    error_message=str(error_details) if not action_success else None
                )
            return
        else:
            print(f"[ENGINE] No active actions found matching postback payload: {payload_str}")

    # Fetch active rules for this seller (for initial triggers)
    rules = AutomationRule.objects.filter(
        seller=seller_account,
        status='active'
    ).order_by('-created_at')

    print(f"[ENGINE] Found {rules.count()} active rules for seller {seller_account.id}")
    for rule in rules:
        print(f"[ENGINE] Evaluating rule: '{rule.name}' (Type: {rule.rule_type}, Match Type: {rule.condition_match_type})")
        logger.info(f"[ENGINE] Evaluating rule: '{rule.name}' (Type: {rule.rule_type}, Match Type: {rule.condition_match_type})")

        # 1. Trigger Type Check
        is_trigger_match = False
        
        # Determine if this event is a story reply
        is_story_reply = (event_type == "STORY_REPLY")
        if not is_story_reply and event_type == "DM":
            meta = interaction.metadata or {}
            reply_to = meta.get("reply_to") or {}
            is_story_reply = bool(reply_to.get("story"))

        if rule.rule_type in ['comment_automation', 'giveaway_comment', 'product_inquiry_comment']:
            is_trigger_match = (event_type == "COMMENT")
            
        elif rule.rule_type in ['story_automation', 'product_inquiry_story']:
            is_trigger_match = is_story_reply
            
        elif rule.rule_type in ['dm_automation', 'giveaway_dm', 'product_inquiry_dm']:
            is_trigger_match = (event_type in ["DM", "CLICK"]) and not is_story_reply

        if not is_trigger_match:
            print(f"[ENGINE - MISMATCH] Rule '{rule.name}' trigger type mismatch. Rule expects trigger for: {rule.rule_type}, Got event: {event_type}")
            logger.info(f"[ENGINE - MISMATCH] Rule '{rule.name}' trigger type mismatch. Rule expects trigger for: {rule.rule_type}, Got event: {event_type}")
            continue

        # 2. Target Mode Check (Specific Post / Reels / Stories)
        if rule.target_mode == "selected":
            clean_media_id = str(media_id).strip() if media_id else ""
            rule_media_ids = [str(mid).strip() for mid in (rule.target_media_ids or [])]
            if not clean_media_id or clean_media_id not in rule_media_ids:
                print(f"[ENGINE - MISMATCH] Rule '{rule.name}' target media ID mismatch. Interaction media: '{clean_media_id}', Rule targets: {rule_media_ids}")
                logger.info(f"[ENGINE - MISMATCH] Rule '{rule.name}' target media ID mismatch. Interaction media: '{clean_media_id}', Rule targets: {rule_media_ids}")
                continue

        # 3. Condition Check (Keywords)
        is_condition_match = False
        match_type = rule.condition_match_type
        keywords = [str(k).strip().lower() for k in (rule.condition_keywords or [])]

        if match_type == "any":
            is_condition_match = True
        elif match_type == "equals":
            is_condition_match = message_text.lower() in keywords
        elif match_type == "contains":
            is_condition_match = any(k in message_text.lower() for k in keywords)

        if not is_condition_match:
            print(f"[ENGINE - MISMATCH] Rule '{rule.name}' condition mismatch. Message: '{message_text}', Match Type: {match_type}, Keywords: {keywords}")
            logger.info(f"[ENGINE - MISMATCH] Rule '{rule.name}' condition mismatch. Message: '{message_text}', Match Type: {match_type}, Keywords: {keywords}")
            continue

        # Rule Matched! Now execute it.
        print(f"[ENGINE] Rule '{rule.name}' matched for interaction {interaction.id}!")
        logger.info(f"[ENGINE] Rule '{rule.name}' matched for interaction {interaction.id}!")

        # Generate unique event hash to prevent duplicate processing
        event_hash_input = f"{rule.id}:{interaction.instagram_event_id or interaction.id}"
        event_hash = hashlib.sha256(event_hash_input.encode('utf-8')).hexdigest()

        # Check if already executed to prevent double fires
        if AutomationExecution.objects.filter(rule=rule, trigger_event_hash=event_hash).exists():
            logger.warning(f"[ENGINE] Duplicate execution detected and blocked for rule {rule.id} and event hash {event_hash}")
            continue

        # 5. Follower Gate Check
        if rule.follower_gate_enabled:
            is_following = customer.is_following_business
            if is_following is False:
                logger.info(f"[ENGINE] Customer {customer.id} does not follow business. Executing follower gate actions.")
                fg_messages = rule.follower_gate_messages or ["Please follow our page to unlock this offer!"]
                msg_text = random.choice(fg_messages)
                
                success, resp = send_instagram_dm(seller_account, customer.instagram_scoped_id, {"text": msg_text}, "text")
                
                AutomationExecution.objects.create(
                    rule=rule,
                    customer=customer,
                    trigger_event_type=event_type,
                    trigger_text=message_text,
                    trigger_media_id=media_id,
                    trigger_event_hash=event_hash,
                    status='skipped',
                    error_message="Follower gate blocked execution. Follower gate warning message sent.",
                    actions_log=[{"action_type": "follower_gate_dm", "status": "sent" if success else "failed", "error": str(resp) if not success else None}]
                )

                continue

        # 6. Execute Actions
        actions_log = []
        overall_status = "success"
        failures = 0
        total_actions = 0

        # Only execute the first-level actions (where parent_event is not set)
        actions = rule.actions.filter(Q(parent_event__isnull=True) | Q(parent_event="")).order_by('order')
        
        for action in actions:
            total_actions += 1
            action_type = action.action_type
            dm_format = action.dm_format or "text"
            
            selected_msg = ""
            if action.messages:
                if action.message_mode == "random":
                    selected_msg = random.choice(action.messages)
                elif action.message_mode == "fixed" or not action.message_mode:
                    selected_msg = action.messages[0]
                elif action.message_mode == "sequential":
                    exec_count = AutomationExecution.objects.filter(rule=rule).count()
                    selected_msg = action.messages[exec_count % len(action.messages)]
            
            action_success = False
            error_details = None

            if action_type == "reply_comment":
                if event_type == "COMMENT":
                    action_success, resp = reply_instagram_comment(
                        seller_account,
                        interaction.instagram_event_id,
                        selected_msg
                    )
                    if not action_success:
                        error_details = resp
                else:
                    error_details = "Cannot reply to comment: Trigger event was not a comment."

            elif action_type in ["send_dm", "reply_story"]:
                # Determine recipient and recipient_type:
                # If the trigger was a comment, we must send a Private Reply using the comment_id.
                recipient_id = customer.instagram_scoped_id
                recipient_type = "id"
                if event_type == "COMMENT" and interaction.instagram_event_id:
                    recipient_id = interaction.instagram_event_id
                    recipient_type = "comment_id"

                if dm_format == "attachment":
                    attachments = action.attachment_payload or []
                    if not attachments:
                        action_success = True
                    else:
                        action_success = True
                        error_details_list = []
                        
                        # Group consecutive images together
                        grouped_runs = []
                        current_image_group = []
                        
                        for att in attachments:
                            att_type = att.get("type", "image")
                            if att_type == "image":
                                current_image_group.append(att)
                            else:
                                if current_image_group:
                                    grouped_runs.append(("image_group", current_image_group))
                                    current_image_group = []
                                grouped_runs.append((att_type, att))
                        if current_image_group:
                            grouped_runs.append(("image_group", current_image_group))
                            
                        for run_type, run_data in grouped_runs:
                            if run_type == "image_group":
                                if len(run_data) > 1:
                                    msg_data = {
                                        "attachment_type": "images",
                                        "urls": [item.get("url", "") for item in run_data]
                                    }
                                else:
                                    msg_data = {
                                        "attachment_type": "image",
                                        "url": run_data[0].get("url", "")
                                    }
                            elif run_type == "sticker":
                                msg_data = {
                                    "attachment_type": "sticker",
                                    "sticker_id": run_data.get("sticker_id", "like_heart")
                                }
                            elif run_type == "MEDIA_SHARE":
                                msg_data = {
                                    "attachment_type": "MEDIA_SHARE",
                                    "media_id": run_data.get("media_id")
                                }
                            else:
                                msg_data = {
                                    "attachment_type": run_type,
                                    "url": run_data.get("url", "")
                                }
                                
                            success, resp = send_instagram_dm(
                                seller_account,
                                recipient_id,
                                msg_data,
                                dm_format=dm_format,
                                recipient_type=recipient_type
                            )
                            if not success:
                                action_success = False
                                error_details_list.append(str(resp))
                        if not action_success:
                            error_details = "; ".join(error_details_list)
                else:
                    msg_data = {"text": selected_msg}
                    if dm_format == "quick_reply":
                        msg_data["quick_replies"] = action.quick_reply_payload.get("quick_replies", [])
                    elif dm_format == "button_template":
                        msg_data["buttons"] = action.button_template_payload.get("buttons", [])
                    elif dm_format == "generic_template":
                        msg_data["elements"] = action.generic_template_payload.get("elements", [])

                    action_success, resp = send_instagram_dm(
                        seller_account,
                        recipient_id,
                        msg_data,
                        dm_format=dm_format,
                        recipient_type=recipient_type
                    )
                    if not action_success:
                        error_details = resp

            actions_log.append({
                "action_type": action_type,
                "dm_format": dm_format,
                "status": "success" if action_success else "failed",
                "message_sent": selected_msg,
                "error": str(error_details) if error_details else None
            })

            if not action_success:
                failures += 1

        if total_actions > 0:
            if failures == total_actions:
                overall_status = "failed"
            elif failures > 0:
                overall_status = "partial"
        else:
            overall_status = "skipped"

        # Save Execution Log
        AutomationExecution.objects.create(
            rule=rule,
            customer=customer,
            trigger_event_type=event_type,
            trigger_text=message_text,
            trigger_media_id=media_id,
            trigger_event_hash=event_hash,
            status=overall_status,
            actions_log=actions_log,
            error_message=f"Executed {total_actions - failures}/{total_actions} actions successfully." if failures > 0 else None
        )


