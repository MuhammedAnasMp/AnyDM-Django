import requests
import json
import logging
import random
import hashlib
from django.utils import timezone
from django.db.models import Q
from apps.automations.models import AutomationRule, AutomationAction, AutomationExecution, AutomationFollowerGain

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
                        formatted_price = f"{int(price_val)}" if price_val.is_integer(
                        ) else f"{price_val:.2f}"
                        price_str = f"₹{formatted_price}"
                    return price_str, product.title
        except Exception as e:
            logger.error(
                f"[ENGINE] Error resolving dynamic details for URL {url}: {e}", exc_info=True)
        return None, None

    def process_button(btn):
        title = btn.get("title", "")
        if "{{price}}" in title or "{{name}}" in title:
            price_str, prod_title = get_product_details_for_url(
                btn.get("url", ""))
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
            url = elem.get("default_action", {}).get(
                "url", "") if elem.get("default_action") else ""
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


def record_outbound_dm_metrics(account, response):
    """
    Tracks rolling DM timestamps (1h, 24h) and caches Meta rate limit usage headers.
    """
    try:
        from django.core.cache import cache
        import time, json
        
        now_ts = time.time()
        key = f"ig_dm_timestamps_{account.id}"
        timestamps = cache.get(key, [])
        cutoff = now_ts - 86400  # retain last 24 hours
        timestamps = [t for t in timestamps if t > cutoff]
        timestamps.append(now_ts)
        cache.set(key, timestamps, timeout=90000)

        # Parse and cache X-Business-Use-Case-Usage headers
        usage_header = response.headers.get("X-Business-Use-Case-Usage")
        if usage_header:
            try:
                usage_json = json.loads(usage_header)
                cache.set(f"ig_rate_limit_usage_{account.id}", usage_json, timeout=3600)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Failed to record outbound DM metrics for account {account.id}: {e}")


def sanitize_meta_button(btn):
    """
    Sanitizes a button dict to strictly match Meta Graph API requirements.
    web_url: type, title, url (NO payload, NO is_profile_button)
    postback: type, title, payload (NO url, NO is_profile_button)
    """
    if not isinstance(btn, dict):
        return None
    b_type = btn.get("type", "web_url")
    if b_type == "product":
        b_type = "web_url"
    title = str(btn.get("title", ""))[:20]

    if b_type == "postback":
        payload = str(btn.get("payload", ""))[:1000]
        if not payload:
            return None
        return {
            "type": "postback",
            "title": title or "Select",
            "payload": payload
        }
    elif b_type == "web_url":
        url = str(btn.get("url", "")).strip()
        if not url:
            return None
        return {
            "type": "web_url",
            "title": title or "Visit",
            "url": url
        }
    return None


def send_instagram_dm(account, recipient_id, message_data, dm_format="text", recipient_type="id"):
    """
    Sends a direct message to a user using the Meta Instagram Messaging API v25.0.
    Supports text, quick replies, button templates, and generic (carousel) templates.
    """
    access_token = account.access_token
    instagram_scoped_id = account.instagram_scoped_id or account.instagram_user_id

    if not access_token or not instagram_scoped_id:
        logger.error(
            f"Cannot send DM: Account {account.id} missing access token or Instagram scoped ID.")
        return False, "Missing credentials"

    url = f"https://graph.instagram.com/v26.0/{instagram_scoped_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Resolve any dynamic {{price}} placeholders in button titles
    try:
        resolve_dynamic_prices(message_data, dm_format)
    except Exception as e:
        logger.error(
            f"[ENGINE] Failed to resolve dynamic prices: {e}", exc_info=True)

    # Build the payload based on dm_format
    message_payload = {}
    if dm_format == "text":
        message_payload = {"text": message_data.get("text", "")}
    elif dm_format == "quick_reply":
        raw_qrs = message_data.get("quick_replies", [])
        clean_qrs = []
        for qr in raw_qrs:
            if isinstance(qr, dict):
                clean_qrs.append({
                    "content_type": qr.get("content_type", "text"),
                    "title": str(qr.get("title", ""))[:20],
                    "payload": str(qr.get("payload", qr.get("title", "")))[:1000]
                })
        message_payload = {
            "text": str(message_data.get("text", ""))[:640],
            "quick_replies": clean_qrs[:13]
        }
    elif dm_format == "button_template":
        raw_buttons = message_data.get("buttons", [])
        clean_buttons = []
        for b in raw_buttons:
            sanitized = sanitize_meta_button(b)
            if sanitized:
                clean_buttons.append(sanitized)
        message_payload = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": str(message_data.get("text", ""))[:640],
                    "buttons": clean_buttons[:3]
                }
            }
        }
    elif dm_format == "generic_template":
        raw_elements = message_data.get("elements", [])
        clean_elements = []
        for elem in raw_elements:
            if not isinstance(elem, dict):
                continue
            clean_elem = {
                "title": str(elem.get("title", ""))[:80]
            }
            if elem.get("subtitle"):
                clean_elem["subtitle"] = str(elem.get("subtitle", ""))[:80]
            if elem.get("image_url"):
                clean_elem["image_url"] = str(elem.get("image_url", ""))
            if elem.get("default_action") and isinstance(elem.get("default_action"), dict):
                def_url = str(elem["default_action"].get("url", "")).strip()
                if def_url:
                    clean_elem["default_action"] = {
                        "type": "web_url",
                        "url": def_url
                    }
            raw_btns = elem.get("buttons", [])
            clean_btns = []
            for b in raw_btns:
                sanitized = sanitize_meta_button(b)
                if sanitized:
                    clean_btns.append(sanitized)
            if clean_btns:
                clean_elem["buttons"] = clean_btns[:3]
            clean_elements.append(clean_elem)
        message_payload = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": clean_elements[:10]
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
    elif dm_format == "show_profile":
        profile_url = message_data.get("profile_url") or f"https://instagram.com/{account.username}"
        button_title = (message_data.get("profile_button_text") or "👤 Visit Profile")[:20]
        header_text = message_data.get("text") or message_data.get("profile_message_text") or f"Check out our Instagram profile:"
        message_payload = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": header_text,
                    "buttons": [
                        {
                            "type": "web_url",
                            "url": profile_url,
                            "title": button_title
                        }
                    ]
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
            print(
                f"\n[DEBUG CURL CALL]:\ncurl -X POST \"{url}\" \\\n  -H \"Authorization: Bearer {access_token}\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{curl_payload}'\n")
        except Exception:
            try:
                print(
                    f"\n[DEBUG CURL CALL]:\ncurl -X POST \"{url}\" ... (payload printed with ascii replacement)")
            except Exception:
                pass

        response = requests.post(url, headers=headers,
                                 json=payload, timeout=15)
        # Record rate limits and hourly velocity
        record_outbound_dm_metrics(account, response)
        response_data = response.json()
        if response.status_code == 200:
            print(
                f"[ENGINE] DM successfully sent to {recipient_id}: {response_data.get('message_id')}")
            logger.info(
                f"DM successfully sent to {recipient_id}: {response_data.get('message_id')}")
            return True, response_data
        else:
            print(
                f"[ENGINE] Failed to send DM to {recipient_id}: Status {response.status_code}, Response: {response_data}")
            logger.error(
                f"Failed to send DM to {recipient_id}: Status {response.status_code}, Response: {response_data}")
            return False, response_data.get("error", {}).get("message", "API Error")
    except Exception as e:
        print(f"[ENGINE] Exception sending DM to {recipient_id}: {e}")
        logger.error(
            f"Exception sending DM to {recipient_id}: {e}", exc_info=True)
        return False, str(e)


def reply_instagram_comment(account, comment_id, message_text):
    """
    Replies to a comment on a post using the Meta Instagram Comment Moderation API v25.0.
    """
    access_token = account.access_token
    if not access_token:
        logger.error(
            f"Cannot reply to comment: Account {account.id} missing access token.")
        return False, "Missing credentials"

    url = f"https://graph.instagram.com/v26.0/{comment_id}/replies"
    params = {"access_token": access_token}
    data = {"message": message_text}

    try:
        response = requests.post(url, params=params, json=data, timeout=15)
        response_data = response.json()
        if response.status_code == 200:
            logger.info(
                f"Comment reply successfully sent for comment {comment_id}: {response_data.get('id')}")
            return True, response_data
        else:
            logger.error(
                f"Failed to reply to comment {comment_id}: Status {response.status_code}, Response: {response_data}")
            return False, response_data.get("error", {}).get("message", "API Error")
    except Exception as e:
        logger.error(
            f"Exception replying to comment {comment_id}: {e}", exc_info=True)
        return False, str(e)


def execute_single_action(seller_account, recipient_id, action, rule, customer, interaction, selected_msg, recipient_type="id", visited_node_ids=None):
    """
    Executes a single AutomationAction or resolves loop_back actions to their target card.
    Returns (action_success, error_details, effective_dm_format, executed_message_text).
    """
    if visited_node_ids is None:
        visited_node_ids = set()

    action_type = action.action_type
    dm_format = action.dm_format or "text"

    if action_type == "reply_comment":
        event_type = interaction.event_type
        if event_type == "COMMENT" and interaction.instagram_event_id:
            success, resp = reply_instagram_comment(
                seller_account,
                interaction.instagram_event_id,
                selected_msg
            )
            return success, (None if success else resp), "text", selected_msg
        else:
            return False, "Cannot reply to comment: Missing comment ID or event is not a comment.", "text", selected_msg

    elif action_type in ["send_dm", "reply_story"]:
        # Handle Loop Back resolution
        if dm_format == "loop_back":
            loop_target_id = action.loop_target_id
            if not loop_target_id:
                root_action = rule.actions.filter(
                    Q(parent_event__isnull=True) | Q(parent_event=""),
                    action_type__in=["send_dm", "reply_story"]
                ).exclude(id=action.id).first()
                if root_action:
                    t_msg = root_action.messages[0] if root_action.messages else selected_msg
                    return execute_single_action(
                        seller_account, recipient_id, root_action, rule, customer, interaction,
                        t_msg, recipient_type=recipient_type, visited_node_ids=visited_node_ids
                    )
                return False, "Loop back target not configured", "loop_back", selected_msg

            if str(loop_target_id) in visited_node_ids:
                logger.warning(f"[ENGINE] Circular loop detected on target {loop_target_id}")
                return False, "Circular loop detected", "loop_back", selected_msg

            visited_node_ids.add(str(loop_target_id))

            # 1. Search in visual_data nodes
            nodes = (rule.visual_data or {}).get("nodes", [])
            target_node = next((n for n in nodes if str(n.get("id")) == str(loop_target_id)), None)

            if target_node:
                t_data = target_node.get("data", {})
                target_format = t_data.get("dm_format", "text")
                t_msgs = t_data.get("messages") or []
                t_msg = t_msgs[0] if t_msgs else t_data.get("text", selected_msg)

                # Recursively resolve if target is another loop_back
                if target_format == "loop_back":
                    mock_action = AutomationAction(
                        rule=rule,
                        action_type="send_dm",
                        dm_format="loop_back",
                        loop_target_id=t_data.get("loop_target_id"),
                        messages=[t_msg]
                    )
                    return execute_single_action(
                        seller_account, recipient_id, mock_action, rule, customer, interaction,
                        t_msg, recipient_type=recipient_type, visited_node_ids=visited_node_ids
                    )

                # Construct msg_data from target_node
                if target_format == "button_template":
                    btns = []
                    btn_json = t_data.get("button_template_buttons_json", "[]")
                    if isinstance(btn_json, str) and btn_json.strip():
                        try:
                            btns = json.loads(btn_json)
                        except Exception:
                            btns = []
                    elif isinstance(btn_json, list):
                        btns = btn_json
                    cleaned_buttons = []
                    for btn in btns:
                        btn_copy = dict(btn)
                        if btn_copy.get("type") == "product":
                            btn_copy["type"] = "web_url"
                        cleaned_buttons.append(btn_copy)
                    msg_data = {"text": t_data.get("button_template_text") or t_msg, "buttons": cleaned_buttons}

                elif target_format == "quick_reply":
                    qr_titles = t_data.get("quick_replies_titles", [])
                    if isinstance(qr_titles, str):
                        qr_titles = [t.strip() for t in qr_titles.split(",") if t.strip()]
                    quick_replies = [
                        {"content_type": "text", "title": t[:20], "payload": f"QR_{t[:20].upper().replace(' ', '_')}"}
                        for t in qr_titles
                    ]
                    msg_data = {"text": t_data.get("quick_reply_text") or t_msg, "quick_replies": quick_replies}

                elif target_format == "generic_template":
                    elems = []
                    elems_json = t_data.get("generic_template_elements_json", "[]")
                    if isinstance(elems_json, str) and elems_json.strip():
                        try:
                            elems = json.loads(elems_json)
                        except Exception:
                            elems = []
                    elif isinstance(elems_json, list):
                        elems = elems_json
                    msg_data = {"elements": elems}

                elif target_format == "show_profile":
                    p_url = t_data.get("profile_url") or f"https://instagram.com/{seller_account.username}"
                    p_btn = (t_data.get("profile_button_text") or "👤 Visit Profile")[:20]
                    p_text = t_data.get("profile_message_text") or t_msg or "Check out our Instagram profile:"
                    msg_data = {
                        "profile_url": p_url,
                        "profile_button_text": p_btn,
                        "profile_message_text": p_text,
                        "text": p_text
                    }

                elif target_format == "check_follow":
                    is_following = customer.is_following_business if customer else False
                    if is_following is True:
                        branch_format = t_data.get("following_format", "text")
                        branch_text = t_data.get("following_text") or "Thanks for following us!"
                        branch_btn_text = t_data.get("following_button_text") or ""
                        branch_btns_json = t_data.get("following_buttons_json") or ""
                        branch_profile_url = t_data.get("following_profile_url") or f"https://instagram.com/{seller_account.username}"
                    else:
                        branch_format = t_data.get("not_following_format", "button_template")
                        branch_text = t_data.get("not_following_text") or "Please follow our Instagram page to access full content!"
                        branch_btn_text = t_data.get("not_following_button_text") or "👉 Follow Us"
                        branch_btns_json = t_data.get("not_following_buttons_json") or ""
                        branch_profile_url = t_data.get("not_following_profile_url") or f"https://instagram.com/{seller_account.username}"

                    target_format = branch_format
                    if branch_format == "show_profile":
                        msg_data = {
                            "profile_url": branch_profile_url,
                            "profile_button_text": branch_btn_text or "👤 Visit Profile",
                            "text": branch_text
                        }
                    elif branch_format == "button_template":
                        btns = []
                        if branch_btns_json:
                            try:
                                btns = json.loads(branch_btns_json) if isinstance(branch_btns_json, str) else branch_btns_json
                            except Exception:
                                btns = []
                        if not btns:
                            btns = [{"type": "web_url", "url": branch_profile_url, "title": (branch_btn_text or "👉 Follow Us")[:20]}]
                        msg_data = {"text": branch_text, "buttons": btns}
                    else:
                        msg_data = {"text": branch_text}

                elif target_format == "attachment":
                    attachments_raw = t_data.get("attachments", [])
                    if isinstance(attachments_raw, str) and attachments_raw.strip():
                        try:
                            attachments_raw = json.loads(attachments_raw)
                        except Exception:
                            attachments_raw = []
                    target_action = AutomationAction(
                        rule=rule, action_type="send_dm", dm_format="attachment",
                        attachment_payload=attachments_raw
                    )
                    return execute_single_action(
                        seller_account, recipient_id, target_action, rule, customer, interaction,
                        t_msg, recipient_type=recipient_type, visited_node_ids=visited_node_ids
                    )

                else:
                    msg_data = {"text": t_msg}

                success, resp = send_instagram_dm(
                    seller_account,
                    recipient_id,
                    msg_data,
                    dm_format=target_format,
                    recipient_type=recipient_type
                )
                return success, (None if success else resp), target_format, t_msg

            # 2. Check if loop_target_id matches an Action in DB
            target_action = None
            if str(loop_target_id).isdigit():
                target_action = rule.actions.filter(id=int(loop_target_id)).first()
            if not target_action:
                for a in rule.actions.all():
                    if f"node-a-{rule.id}-{a.id}" == str(loop_target_id) or str(a.id) in str(loop_target_id):
                        target_action = a
                        break

            if target_action:
                t_msg = target_action.messages[0] if target_action.messages else selected_msg
                return execute_single_action(
                    seller_account, recipient_id, target_action, rule, customer, interaction,
                    t_msg, recipient_type=recipient_type, visited_node_ids=visited_node_ids
                )

            # 3. Fallback to sending primary DM
            root_action = rule.actions.filter(
                Q(parent_event__isnull=True) | Q(parent_event=""),
                action_type__in=["send_dm", "reply_story"]
            ).exclude(id=action.id).first()
            if root_action:
                t_msg = root_action.messages[0] if root_action.messages else selected_msg
                return execute_single_action(
                    seller_account, recipient_id, root_action, rule, customer, interaction,
                    t_msg, recipient_type=recipient_type, visited_node_ids=visited_node_ids
                )

            return False, f"Loop back target '{loop_target_id}' could not be resolved.", "loop_back", selected_msg

        # Standard DM Formats
        if dm_format == "attachment":
            attachments = action.attachment_payload or []
            if not attachments:
                return True, None, dm_format, selected_msg

            action_success = True
            error_details_list = []
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

            return action_success, ("; ".join(error_details_list) if not action_success else None), dm_format, selected_msg

        else:
            msg_data = {"text": selected_msg}
            if dm_format == "quick_reply":
                msg_data["quick_replies"] = action.quick_reply_payload.get("quick_replies", [])
            elif dm_format == "button_template":
                msg_data["buttons"] = action.button_template_payload.get("buttons", [])
            elif dm_format == "generic_template":
                msg_data["elements"] = action.generic_template_payload.get("elements", [])
            elif dm_format == "show_profile":
                payload = action.show_profile_payload or {}
                msg_data = {
                    "profile_url": payload.get("profile_url") or f"https://instagram.com/{seller_account.username}",
                    "profile_button_text": (payload.get("profile_button_text") or "👤 Visit Profile")[:20],
                    "profile_message_text": payload.get("profile_message_text") or selected_msg or "Check out our Instagram profile:",
                    "text": payload.get("profile_message_text") or selected_msg or "Check out our Instagram profile:"
                }
            elif dm_format == "check_follow":
                cf_payload = action.check_follow_payload or {}
                is_following = customer.is_following_business if customer else False
                if is_following is None and customer:
                    try:
                        from apps.crm.utils import sync_customer_profile
                        sync_customer_profile(customer, force=True)
                        is_following = customer.is_following_business
                    except Exception as e:
                        logger.error(f"[ENGINE] Failed to sync customer for check_follow: {e}")

                if is_following is True:
                    branch_format = cf_payload.get("following_format", "text")
                    branch_text = cf_payload.get("following_text") or "Thanks for following us!"
                    branch_button_text = cf_payload.get("following_button_text") or ""
                    branch_buttons_json = cf_payload.get("following_buttons_json") or ""
                    branch_profile_url = cf_payload.get("following_profile_url") or f"https://instagram.com/{seller_account.username}"
                else:
                    branch_format = cf_payload.get("not_following_format", "button_template")
                    branch_text = cf_payload.get("not_following_text") or "Please follow our Instagram page to access full content!"
                    branch_button_text = cf_payload.get("not_following_button_text") or "👉 Follow Us"
                    branch_buttons_json = cf_payload.get("not_following_buttons_json") or ""
                    branch_profile_url = cf_payload.get("not_following_profile_url") or f"https://instagram.com/{seller_account.username}"

                dm_format = branch_format
                if branch_format == "show_profile":
                    msg_data = {
                        "profile_url": branch_profile_url,
                        "profile_button_text": branch_button_text or "👤 Visit Profile",
                        "text": branch_text
                    }
                elif branch_format == "button_template":
                    btns = []
                    if branch_buttons_json:
                        try:
                            btns = json.loads(branch_buttons_json) if isinstance(branch_buttons_json, str) else branch_buttons_json
                        except Exception:
                            btns = []
                    if not btns:
                        btns = [{
                            "type": "web_url",
                            "url": branch_profile_url,
                            "title": (branch_button_text or "👉 Follow Us")[:20]
                        }]
                    msg_data = {
                        "text": branch_text,
                        "buttons": btns
                    }
                else:
                    msg_data = {"text": branch_text}

            success, resp = send_instagram_dm(
                seller_account,
                recipient_id,
                msg_data,
                dm_format=dm_format,
                recipient_type=recipient_type
            )
            return success, (None if success else resp), dm_format, selected_msg

    return False, f"Unsupported action type: {action_type}", dm_format, selected_msg


def execute_automation(interaction):
    """
    Processes an inbound customer interaction, matches it to active automation rules,
    and executes the corresponding actions. Supports multi-level flow graph traversal.
    """
    print(
        f"[ENGINE] execute_automation called. ID={interaction.id}, Direction={interaction.direction}, Event={interaction.event_type}, Text='{interaction.message_text}'")
    is_echo = (interaction.metadata or {}).get("is_echo", False)
    if is_echo or interaction.direction != "INBOUND":
        print(
            f"[ENGINE] Ignored: Direction is not INBOUND or is echo (direction={interaction.direction}, is_echo={is_echo})")
        return

    seller_account = interaction.seller_account
    if seller_account and not seller_account.is_enabled:
        print(f"[ENGINE] Ignored: Seller account {seller_account.username} (ID: {seller_account.id}) is not enabled.")
        logger.info(f"[ENGINE] Ignored: Seller account {seller_account.username} (ID: {seller_account.id}) is not enabled.")
        return

    customer = interaction.customer
    message_text = (interaction.message_text or "").strip()
    event_type = interaction.event_type  # DM, COMMENT, CLICK (postback)
    media_id = interaction.media_id

    print(
        f"[ENGINE] Processing interaction {interaction.id} from customer {customer.id} (username: {customer.username}). Event: {event_type}, Msg: '{message_text}', Media: '{media_id}'")
    logger.info(
        f"[ENGINE] Processing interaction {interaction.id} from customer {customer.id} (username: {customer.username}). Event: {event_type}, Msg: '{message_text}', Media: '{media_id}'")

    # ─────────────────────────────────────────────────────────────────────────
    # ORDER TRACKING DYNAMIC AUTOMATION
    # ─────────────────────────────────────────────────────────────────────────
    payload_str = ""
    if event_type == "CLICK":
        payload_str = (interaction.metadata or {}).get(
            "postback", {}).get("payload", "")
        if not payload_str:
            payload_str = (interaction.metadata or {}).get(
                "quick_reply", {}).get("payload", "")
        if not payload_str and message_text.startswith("Postback: "):
            payload_str = message_text.replace("Postback: ", "")

    if customer.waiting_for_order_id and event_type == "DM" and message_text:
        # Check if tracking is still enabled/active
        has_track_order_enabled = False
        active_rules = AutomationRule.objects.filter(seller=seller_account, status='active')
        for r in active_rules:
            if r.visual_data and "TRACK_ORDER" in json.dumps(r.visual_data):
                has_track_order_enabled = True
                break
                
        if not has_track_order_enabled:
            inactive_rules = AutomationRule.objects.filter(seller=seller_account).exclude(status='active')
            has_inactive_track = False
            for r in inactive_rules:
                if r.visual_data and "TRACK_ORDER" in json.dumps(r.visual_data):
                    has_inactive_track = True
                    break
            
            if not has_inactive_track:
                from apps.accounts.models import WebsiteSettings
                settings = WebsiteSettings.objects.filter(instagram_account=seller_account).first()
                if settings and settings.custom_settings and "TRACK_ORDER" in json.dumps(settings.custom_settings):
                    has_track_order_enabled = True
                    
        if not has_track_order_enabled:
            # Order tracking has been disabled by the seller, reset customer state and do not reply
            customer.waiting_for_order_id = False
            customer.order_track_retry_count = 0
            customer.save()
            return
        else:
            clean_text = message_text.strip()

            # Check for cancel keywords
            if clean_text.lower() in ["cancel", "exit", "stop"]:
                customer.waiting_for_order_id = False
                customer.save()
                msg = "❌ Order tracking cancelled. How else can I help you today?"
                send_instagram_dm(seller_account, customer.instagram_scoped_id, {
                                  "text": msg}, "text")
                return

            # Search for order
            from apps.crm.models import Order
            order = Order.objects.filter(
                order_id__iexact=clean_text, instagram_account=seller_account).first()
            if not order and clean_text.startswith("#"):
                order = Order.objects.filter(
                    order_id__iexact=clean_text[1:], instagram_account=seller_account).first()

            if order:
                customer.waiting_for_order_id = False
                customer.order_track_retry_count = 0
                customer.save()

                status_display = order.get_order_status_display()
                items = order.items.all()
                items_str = ""
                for item in items:
                    items_str += f"- {item.product.title} (x{item.quantity})\n"

                msg = (
                    f"📦 Order Details for #{order.order_id}:\n"
                    f"👤 Customer: {order.customer_name}\n"
                    f"📅 Order Date: {order.created_at.strftime('%d-%b-%Y')}\n"
                    f"🚚 Status: {status_display}\n"
                    f"💰 Total Amount: ₹{order.total_amount}\n"
                    f"💳 Payment: {order.payment_method} ({order.payment_status})\n"
                )
                if items_str:
                    msg += f"\n🛒 Items:\n{items_str}"

                send_instagram_dm(seller_account, customer.instagram_scoped_id, {
                                  "text": msg}, "text")
                return
            else:
                customer.order_track_retry_count += 1
                customer.save()

                # Fetch retry limit from WebsiteSettings
                from apps.accounts.models import WebsiteSettings
                settings = WebsiteSettings.objects.filter(
                    instagram_account=seller_account).first()
                retry_limit = 3  # default fallback
                if settings and settings.custom_settings:
                    retry_limit = settings.custom_settings.get(
                        "order_track_retry_limit", 3)
                    try:
                        retry_limit = int(retry_limit)
                    except Exception:
                        retry_limit = 3

                if customer.order_track_retry_count >= retry_limit:
                    customer.waiting_for_order_id = False
                    customer.order_track_retry_count = 0
                    customer.save()

                    msg = (
                        f"❌ We couldn't find an order with ID '{clean_text}'.\n\n"
                        f"You have reached the maximum retry limit ({retry_limit} attempts). "
                        f"Order tracking session has been closed."
                    )
                else:
                    remaining_attempts = retry_limit - customer.order_track_retry_count
                    msg = (
                        f"❌ We couldn't find an order with ID '{clean_text}'.\n\n"
                        f"Please double-check your Order ID and reply again ({remaining_attempts} attempts remaining).\n"
                        f"Type 'cancel' to exit order tracking."
                    )

                send_instagram_dm(seller_account, customer.instagram_scoped_id, {
                                  "text": msg}, "text")
                return

    # 2. Trigger order tracking flow if keyword or payload clicked
    is_track_trigger = (payload_str == "TRACK_ORDER") or (
        (event_type in ["DM", "COMMENT"]
         ) and message_text.lower().strip() == "track order"
    )

    if is_track_trigger:
        from apps.accounts.models import WebsiteSettings

        has_track_order_enabled = False
        active_rules = AutomationRule.objects.filter(
            seller=seller_account, status='active')
        for r in active_rules:
            if r.visual_data and "TRACK_ORDER" in json.dumps(r.visual_data):
                has_track_order_enabled = True
                break

        if not has_track_order_enabled:
            inactive_rules = AutomationRule.objects.filter(seller=seller_account).exclude(status='active')
            has_inactive_track = False
            for r in inactive_rules:
                if r.visual_data and "TRACK_ORDER" in json.dumps(r.visual_data):
                    has_inactive_track = True
                    break
            
            if not has_inactive_track:
                settings = WebsiteSettings.objects.filter(
                    instagram_account=seller_account).first()
                if settings and settings.custom_settings and "TRACK_ORDER" in json.dumps(settings.custom_settings):
                    has_track_order_enabled = True

        if not has_track_order_enabled:
            print(
                f"[ENGINE] Ignoring track order trigger: seller {seller_account.username} has not configured it.")
            return
        else:
            customer.waiting_for_order_id = True
            customer.order_track_retry_count = 0
            customer.save()

            # If triggered via COMMENT, send the prompt as a Private Reply DM!
            recipient_id = customer.instagram_scoped_id
            recipient_type = "id"
            if event_type == "COMMENT" and interaction.instagram_event_id:
                recipient_id = interaction.instagram_event_id
                recipient_type = "comment_id"

            msg = "Please reply with your Order ID to track your order. 📦"
            send_instagram_dm(
                seller_account,
                recipient_id,
                {"text": msg},
                dm_format="text",
                recipient_type=recipient_type
            )
            return

    # ─────────────────────────────────────────────────────────────────────────
    # HANDLE MULTI-LEVEL FLOW BUTTON/QUICK-REPLY CLICKS (POSTBACK)
    # ─────────────────────────────────────────────────────────────────────────
    payload_str = ""
    if event_type == "CLICK":
        payload_str = (interaction.metadata or {}).get(
            "postback", {}).get("payload", "")
        if not payload_str:
            payload_str = (interaction.metadata or {}).get(
                "quick_reply", {}).get("payload", "")
        if not payload_str and message_text.startswith("Postback: "):
            payload_str = message_text.replace("Postback: ", "")

    if event_type == "CLICK" and payload_str:
        print(f"[ENGINE] Handling postback click with payload: {payload_str}")
        logger.info(
            f"[ENGINE] Handling postback click with payload: {payload_str}")

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
            print(
                f"[ENGINE] Found {matching_actions.count()} matching actions for postback {payload_str}.")
            now_dt = timezone.now()
            for action in matching_actions:
                rule = action.rule
                if rule.start_at and now_dt < rule.start_at:
                    print(
                        f"[ENGINE - SCHEDULE] Rule '{rule.name}' has not started yet. Starts at: {rule.start_at}")
                    logger.info(
                        f"[ENGINE - SCHEDULE] Rule '{rule.name}' has not started yet. Starts at: {rule.start_at}")
                    continue
                if rule.end_at and now_dt > rule.end_at:
                    print(
                        f"[ENGINE - SCHEDULE] Rule '{rule.name}' has expired. Expired at: {rule.end_at}. Completing rule.")
                    logger.info(
                        f"[ENGINE - SCHEDULE] Rule '{rule.name}' has expired. Expired at: {rule.end_at}. Completing rule.")
                    rule.status = 'completed'
                    rule.save()
                    continue
                # Check Follower Branch filtering (for check_follow branch flows)
                cf_branch = (action.check_follow_payload or {}).get('cf_branch')
                if not cf_branch and action.parent_event == 'CHECK_FOLLOW':
                    if action.dm_format == 'loop_back':
                        cf_branch = 'not_following'
                    elif action.dm_format != 'show_profile':
                        cf_branch = 'following'

                if cf_branch and customer:
                    try:
                        from apps.crm.utils import sync_customer_profile
                        sync_customer_profile(customer, force=True)
                    except Exception as e:
                        logger.warning(f"[ENGINE] Failed to sync customer profile for check_follow branch: {e}")

                    is_following = bool(customer.is_following_business)
                    if cf_branch == 'following' and not is_following:
                        print(f"[ENGINE - CF_BRANCH] Skipping action {action.id} because customer {customer.id} is not following.")
                        logger.info(f"[ENGINE - CF_BRANCH] Skipping action {action.id} because customer {customer.id} is not following.")
                        continue
                    elif cf_branch == 'not_following' and is_following:
                        print(f"[ENGINE - CF_BRANCH] Skipping action {action.id} because customer {customer.id} is already following.")
                        logger.info(f"[ENGINE - CF_BRANCH] Skipping action {action.id} because customer {customer.id} is already following.")
                        continue

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
                        exec_count = AutomationExecution.objects.filter(
                            rule=rule).count()
                        selected_msg = action.messages[exec_count % len(
                            action.messages)]

                recipient_id = customer.instagram_scoped_id
                recipient_type = "id"

                action_success, error_details, effective_dm_format, executed_msg = execute_single_action(
                    seller_account=seller_account,
                    recipient_id=recipient_id,
                    action=action,
                    rule=rule,
                    customer=customer,
                    interaction=interaction,
                    selected_msg=selected_msg,
                    recipient_type=recipient_type
                )

                # Log execution specifically for this action
                event_hash_input = f"{rule.id}:{interaction.instagram_event_id or interaction.id}:{action.id}"
                event_hash = hashlib.sha256(
                    event_hash_input.encode('utf-8')).hexdigest()

                AutomationExecution.objects.create(
                    rule=rule,
                    customer=customer,
                    trigger_event_type=event_type,
                    trigger_text=message_text,
                    trigger_media_id=media_id,
                    trigger_event_hash=event_hash,
                    status="success" if action_success else "failed",
                    actions_log=[{
                        "action_type": action.action_type,
                        "dm_format": effective_dm_format,
                        "status": "success" if action_success else "failed",
                        "message_sent": executed_msg,
                        "error": str(error_details) if error_details else None
                    }],
                    error_message=str(
                        error_details) if not action_success else None
                )
            return
        else:
            print(
                f"[ENGINE] No active actions found matching postback payload: {payload_str}")

    # Fetch active rules for this seller (for initial triggers)
    rules = AutomationRule.objects.filter(
        seller=seller_account,
        status='active'
    ).order_by('-created_at')

    print(
        f"[ENGINE] Found {rules.count()} active rules for seller {seller_account.id}")
    now_dt = timezone.now()
    for rule in rules:
        print(
            f"[ENGINE] Evaluating rule: '{rule.name}' (Type: {rule.rule_type}, Match Type: {rule.condition_match_type})")
        logger.info(
            f"[ENGINE] Evaluating rule: '{rule.name}' (Type: {rule.rule_type}, Match Type: {rule.condition_match_type})")

        # Check schedule
        if rule.start_at and now_dt < rule.start_at:
            print(
                f"[ENGINE - SCHEDULE] Rule '{rule.name}' has not started yet. Starts at: {rule.start_at}")
            logger.info(
                f"[ENGINE - SCHEDULE] Rule '{rule.name}' has not started yet. Starts at: {rule.start_at}")
            continue
        if rule.end_at and now_dt > rule.end_at:
            print(
                f"[ENGINE - SCHEDULE] Rule '{rule.name}' has expired. Expired at: {rule.end_at}. Completing rule.")
            logger.info(
                f"[ENGINE - SCHEDULE] Rule '{rule.name}' has expired. Expired at: {rule.end_at}. Completing rule.")
            rule.status = 'completed'
            rule.save()
            continue

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

        elif rule.rule_type in ['media_share_dm', 'user_share_post_dm']:
            is_media_share_event = (
                interaction.message_type in ["REEL", "POST", "CAROUSEL"] or
                bool(media_id) or
                bool((interaction.metadata or {}).get("attachments"))
            )
            is_trigger_match = (event_type == "DM") and is_media_share_event and not is_story_reply

        elif rule.rule_type in ['dm_automation', 'giveaway_dm', 'product_inquiry_dm']:
            is_trigger_match = (
                event_type in ["DM", "CLICK"]) and not is_story_reply

        if not is_trigger_match:
            print(
                f"[ENGINE - MISMATCH] Rule '{rule.name}' trigger type mismatch. Rule expects trigger for: {rule.rule_type}, Got event: {event_type}")
            logger.info(
                f"[ENGINE - MISMATCH] Rule '{rule.name}' trigger type mismatch. Rule expects trigger for: {rule.rule_type}, Got event: {event_type}")
            continue

        # 2. Target Mode Check (Specific Post / Reels / Stories)
        if rule.target_mode == "selected":
            clean_media_id = str(media_id).strip() if media_id else ""
            rule_media_ids = [str(mid).strip()
                              for mid in (rule.target_media_ids or [])]
            if not clean_media_id or clean_media_id not in rule_media_ids:
                print(
                    f"[ENGINE - MISMATCH] Rule '{rule.name}' target media ID mismatch. Interaction media: '{clean_media_id}', Rule targets: {rule_media_ids}")
                logger.info(
                    f"[ENGINE - MISMATCH] Rule '{rule.name}' target media ID mismatch. Interaction media: '{clean_media_id}', Rule targets: {rule_media_ids}")
                continue

        # 3. Condition Check (Keywords)
        is_condition_match = False
        match_type = rule.condition_match_type
        keywords = [str(k).strip().lower()
                    for k in (rule.condition_keywords or [])]

        if rule.rule_type in ['media_share_dm', 'user_share_post_dm']:
            # Media share automations trigger directly on media share without requiring keyword match
            is_condition_match = True
        elif match_type == "any":
            is_condition_match = True
        elif match_type == "equals":
            is_condition_match = message_text.lower() in keywords
        elif match_type == "contains":
            is_condition_match = any(k in message_text.lower()
                                     for k in keywords)

        if not is_condition_match:
            print(
                f"[ENGINE - MISMATCH] Rule '{rule.name}' condition mismatch. Message: '{message_text}', Match Type: {match_type}, Keywords: {keywords}")
            logger.info(
                f"[ENGINE - MISMATCH] Rule '{rule.name}' condition mismatch. Message: '{message_text}', Match Type: {match_type}, Keywords: {keywords}")
            continue

        # Rule Matched! Now execute it.
        print(
            f"[ENGINE] Rule '{rule.name}' matched for interaction {interaction.id}!")
        logger.info(
            f"[ENGINE] Rule '{rule.name}' matched for interaction {interaction.id}!")

        # Generate unique event hash to prevent duplicate processing
        event_hash_input = f"{rule.id}:{interaction.instagram_event_id or interaction.id}"
        event_hash = hashlib.sha256(
            event_hash_input.encode('utf-8')).hexdigest()

        # Check if already executed to prevent double fires
        if AutomationExecution.objects.filter(rule=rule, trigger_event_hash=event_hash).exists():
            logger.warning(
                f"[ENGINE] Duplicate execution detected and blocked for rule {rule.id} and event hash {event_hash}")
            continue

        # 5. Follower Gate Check
        if rule.follower_gate_enabled:
            try:
                from apps.crm.utils import sync_customer_profile
                sync_customer_profile(customer, force=True)
            except Exception as e:
                logger.error(f"[ENGINE] Failed to sync customer profile for follower gate: {e}", exc_info=True)

            is_following = customer.is_following_business
            if is_following is None:
                logger.info(
                    f"[ENGINE] Customer {customer.id} follower status is unknown (is_following: None due to Instagram API consent restriction). Bypassing gate to avoid blocking followers.")
            elif is_following is False:
                logger.info(
                    f"[ENGINE] Customer {customer.id} does not follow business (is_following: False). Executing follower gate actions.")
                fg_messages = [msg for msg in (
                    rule.follower_gate_messages or []) if str(msg).strip()]

                if not fg_messages:
                    logger.info(
                        f"[ENGINE] Follower gate list is empty. Doing nothing.")
                    AutomationExecution.objects.create(
                        rule=rule,
                        customer=customer,
                        trigger_event_type=event_type,
                        trigger_text=message_text,
                        trigger_media_id=media_id,
                        trigger_event_hash=event_hash,
                        status='skipped',
                        error_message="Follower gate blocked execution. No warning message configured.",
                        actions_log=[]
                    )
                    continue

                msg_text = random.choice(fg_messages)

                if event_type == 'COMMENT' and interaction.instagram_event_id:
                    success, resp = reply_instagram_comment(
                        seller_account,
                        interaction.instagram_event_id,
                        msg_text
                    )
                    action_type = "follower_gate_comment"
                else:
                    success, resp = send_instagram_dm(
                        seller_account,
                        customer.instagram_scoped_id,
                        {"text": msg_text},
                        "text"
                    )
                    action_type = "follower_gate_dm"

                AutomationExecution.objects.create(
                    rule=rule,
                    customer=customer,
                    trigger_event_type=event_type,
                    trigger_text=message_text,
                    trigger_media_id=media_id,
                    trigger_event_hash=event_hash,
                    status='skipped',
                    error_message=f"Follower gate blocked execution. Follower gate warning message sent via {action_type.split('_')[-1]}.",
                    actions_log=[{"action_type": action_type, "status": "sent" if success else "failed", "error": str(
                        resp) if not success else None}]
                )

                continue

        # 6. Execute Actions
        actions_log = []
        overall_status = "success"
        failures = 0
        total_actions = 0

        # Only execute the first-level actions (where parent_event is not set)
        actions = rule.actions.filter(
            Q(parent_event__isnull=True) | Q(parent_event="")).order_by('order')

        for action in actions:
            total_actions += 1
            action_type = action.action_type

            selected_msg = ""
            if action.messages:
                if action.message_mode == "random":
                    selected_msg = random.choice(action.messages)
                elif action.message_mode == "fixed" or not action.message_mode:
                    selected_msg = action.messages[0]
                elif action.message_mode == "sequential":
                    exec_count = AutomationExecution.objects.filter(
                        rule=rule).count()
                    selected_msg = action.messages[exec_count % len(
                        action.messages)]

            recipient_id = customer.instagram_scoped_id
            recipient_type = "id"
            if event_type == "COMMENT" and interaction.instagram_event_id:
                recipient_id = interaction.instagram_event_id
                recipient_type = "comment_id"

            action_success, error_details, effective_dm_format, executed_msg = execute_single_action(
                seller_account=seller_account,
                recipient_id=recipient_id,
                action=action,
                rule=rule,
                customer=customer,
                interaction=interaction,
                selected_msg=selected_msg,
                recipient_type=recipient_type
            )

            actions_log.append({
                "action_type": action_type,
                "dm_format": effective_dm_format,
                "status": "success" if action_success else "failed",
                "message_sent": executed_msg,
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

        if overall_status == "success" and customer and customer.is_following_business:
            try:
                AutomationFollowerGain.objects.get_or_create(
                    rule=rule, customer=customer, defaults={'source': 'automation_interaction'}
                )
            except Exception as e:
                logger.error(f"[ENGINE] Failed to record follower gain: {e}")
