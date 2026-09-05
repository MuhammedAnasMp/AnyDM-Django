from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.shortcuts import get_object_or_404
from apps.accounts.models import InstagramAccount
from apps.automations.models import AutomationRule, AutomationAction, GiveawayConfig, GiveawayReward, AutomationExecution, AutomationFollowerGain
from apps.crm.tasks import fake_redis_task
from django.http import JsonResponse


import json
import logging

logger = logging.getLogger(__name__)


def find_button_title_in_node_data(data, target_payload):
    if not target_payload or not isinstance(data, dict):
        return None
    # Check button_template_buttons_json
    btns_json = data.get('button_template_buttons_json')
    if btns_json:
        try:
            btns = json.loads(btns_json) if isinstance(btns_json, str) else btns_json
            if isinstance(btns, list):
                for b in btns:
                    if isinstance(b, dict) and b.get('payload') == target_payload:
                        return b.get('title') or target_payload
        except Exception:
            pass
    # Check buttons
    btns = data.get('buttons')
    if isinstance(btns, list):
        for b in btns:
            if isinstance(b, dict) and b.get('payload') == target_payload:
                return b.get('title') or target_payload
    # Check not_following_buttons_json / following_buttons_json
    for key in ['not_following_buttons_json', 'following_buttons_json']:
        val = data.get(key)
        if val:
            try:
                btns = json.loads(val) if isinstance(val, str) else val
                if isinstance(btns, list):
                    for b in btns:
                        if isinstance(b, dict) and b.get('payload') == target_payload:
                            return b.get('title') or target_payload
            except Exception:
                pass
    # Check generic_template_elements_json
    elems_json = data.get('generic_template_elements_json')
    if elems_json:
        try:
            elems = json.loads(elems_json) if isinstance(elems_json, str) else elems_json
            if isinstance(elems, list):
                for el in elems:
                    for b in el.get('buttons', []):
                        if isinstance(b, dict) and b.get('payload') == target_payload:
                            return b.get('title') or target_payload
        except Exception:
            pass
    # Check quick_replies_titles or quick_replies
    qrs = data.get('quick_replies_titles') or data.get('quick_replies')
    if isinstance(qrs, list):
        for qr in qrs:
            if isinstance(qr, str) and qr == target_payload:
                return qr
            elif isinstance(qr, dict) and (qr.get('payload') == target_payload or qr.get('title') == target_payload):
                return qr.get('title') or target_payload
    return None


def find_button_title_in_action(action, target_payload):
    if not target_payload:
        return None
    if action.button_template_payload and action.button_template_payload.get('buttons'):
        for b in action.button_template_payload.get('buttons', []):
            if isinstance(b, dict) and b.get('payload') == target_payload:
                return b.get('title') or target_payload
    if action.check_follow_payload:
        for key in ['following_buttons_json', 'not_following_buttons_json']:
            raw = action.check_follow_payload.get(key)
            if raw:
                try:
                    btns = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(btns, list):
                        for b in btns:
                            if isinstance(b, dict) and b.get('payload') == target_payload:
                                return b.get('title') or target_payload
                except Exception:
                    pass
    if action.generic_template_payload and action.generic_template_payload.get('elements'):
        for el in action.generic_template_payload.get('elements', []):
            for b in el.get('buttons', []):
                if isinstance(b, dict) and b.get('payload') == target_payload:
                    return b.get('title') or target_payload
    if action.quick_reply_payload and action.quick_reply_payload.get('quick_replies'):
        for qr in action.quick_reply_payload.get('quick_replies', []):
            if isinstance(qr, dict) and (qr.get('payload') == target_payload or qr.get('title') == target_payload):
                return qr.get('title') or target_payload
    return None


def is_loop_return_edge(e):
    if not isinstance(e, dict):
        return False
    e_id = str(e.get('id', ''))
    e_lbl = str(e.get('label', ''))
    return e_id.startswith('edge-loop') or 'edge-loop-' in e_id or e_lbl == '🔄 Loop Back'


def build_visual_data_from_rule(rule):
    """
    Constructs React Flow visual_data (nodes and edges) from an AutomationRule and its related AutomationActions.
    Ensures any rule (including loop back branches, postbacks, and auto-created rules) loads seamlessly in the visual builder with all required edges.
    """
    if rule.visual_data and isinstance(rule.visual_data, dict):
        nodes = rule.visual_data.get('nodes', [])
        if isinstance(nodes, list) and len(nodes) > 0:
            edges = list(rule.visual_data.get('edges', []))
            modified = False

            # Clean up any stray parent_event on root action nodes
            action_node_ids = {n.get('id') for n in nodes if n.get('type') == 'action'}
            child_node_ids = set()
            for e in edges:
                src = e.get('source')
                tgt = e.get('target')
                if tgt in action_node_ids:
                    if src in action_node_ids and not is_loop_return_edge(e):
                        child_node_ids.add(tgt)
                    elif '-cf-fork' in str(src) or 'fork' in str(src):
                        child_node_ids.add(tgt)

            for node in nodes:
                if node.get('type') == 'action' and node.get('id') not in child_node_ids:
                    if node.get('data', {}).get('parent_event'):
                        node['data']['parent_event'] = None
                        modified = True

            for node in nodes:
                n_id = node.get('id')
                n_data = node.get('data', {}) if isinstance(node.get('data'), dict) else {}
                parent_event = n_data.get('parent_event')
                dm_format = n_data.get('dm_format')
                loop_target_id = n_data.get('loop_target_id')

                # 1. Ensure incoming forward execution edge exists for branch nodes
                if parent_event:
                    has_forward_edge = any(
                        e.get('target') == n_id and not (
                            'loop' in str(e.get('id', '')).lower() or 'loop' in str(e.get('label', '')).lower()
                        )
                        for e in edges
                    )
                    if not has_forward_edge:
                        for pnode in nodes:
                            p_id = pnode.get('id')
                            p_data = pnode.get('data', {}) if isinstance(pnode.get('data'), dict) else {}
                            btn_title = find_button_title_in_node_data(p_data, parent_event)
                            if btn_title:
                                edges.append({
                                    "id": f"edge-reply-{p_id}-{n_id}",
                                    "source": p_id,
                                    "target": n_id,
                                    "label": btn_title or n_data.get('parent_label') or parent_event
                                })
                                modified = True
                                break

                # 2. Ensure loop back return edge exists
                if dm_format == 'loop_back' and loop_target_id:
                    has_loop_edge = any(
                        e.get('source') == n_id and e.get('target') == loop_target_id and (
                            'loop' in str(e.get('id', '')).lower() or 'loop' in str(e.get('label', '')).lower()
                        )
                        for e in edges
                    )
                    if not has_loop_edge:
                        edges.append({
                            "id": f"edge-loop-{n_id}-{loop_target_id}",
                            "source": n_id,
                            "target": loop_target_id,
                            "label": "🔄 Loop Back",
                            "style": {"strokeDasharray": "5,5", "stroke": "#8b5cf6"}
                        })
                        modified = True

            if modified:
                rule.visual_data['edges'] = edges
                rule.visual_data['nodes'] = nodes
                try:
                    rule.save(update_fields=['visual_data'])
                except Exception:
                    pass

            return rule.visual_data

    nodes = []
    edges = []

    rule_type = rule.rule_type or 'comment_automation'
    target_mode = rule.target_mode or 'selected'
    target_media_ids = rule.target_media_ids or []
    target_media_type = rule.target_media_type or ('reel' if rule_type == 'reel_automation' else ('story' if rule_type == 'story_automation' else 'post'))

    # 1. Trigger Node
    t_id = f"node-t-{rule.id}"
    nodes.append({
        "id": t_id,
        "type": "trigger",
        "position": {"x": 80, "y": 150},
        "ruleType": rule_type,
        "data": {
            "target_mode": target_mode,
            "media_ids": target_media_ids,
            "media_type": target_media_type,
            "start_at": rule.start_at.isoformat() if rule.start_at else None,
            "end_at": rule.end_at.isoformat() if rule.end_at else None,
        }
    })

    # 2. Condition Node
    is_share = 'share' in rule_type
    parent_id = t_id

    if not is_share:
        c_id = f"node-c-{rule.id}"
        match_type = rule.condition_match_type or 'contains'
        keywords = rule.condition_keywords or []
        nodes.append({
            "id": c_id,
            "type": "condition",
            "position": {"x": 440, "y": 150},
            "ruleType": rule_type,
            "data": {
                "match_type": match_type,
                "keywords": keywords,
                "keywords_contains": keywords if match_type == 'contains' else [],
                "keywords_equals": keywords if match_type == 'equals' else [],
                "follower_gate_enabled": bool(rule.follower_gate_enabled),
                "follower_gate_messages": rule.follower_gate_messages or []
            }
        })
        edges.append({
            "id": f"edge-{rule.id}-tc",
            "source": t_id,
            "target": c_id
        })
        parent_id = c_id

    # 3. Action Nodes
    actions = list(rule.actions.all().order_by('order'))
    if not actions:
        a_id = f"node-a-{rule.id}-0"
        nodes.append({
            "id": a_id,
            "type": "action",
            "position": {"x": 440 if is_share else 800, "y": 150},
            "ruleType": rule_type,
            "data": {
                "action_type": "send_dm",
                "dm_format": "text",
                "messages": ["Sent you a DM! Check your inbox."],
                "is_placeholder": False,
                "isPrimary": True,
                "action_label": "DIRECT MESSAGE"
            }
        })
        edges.append({
            "id": f"edge-{rule.id}-act-0",
            "source": parent_id,
            "target": a_id
        })
    else:
        created_action_nodes = []
        for i, action in enumerate(actions):
            a_id = f"node-a-{rule.id}-{action.id or i}"
            pos_y = 80 + (i * 320)
            is_primary = action.action_type == 'send_dm'
            act_label = "DIRECT MESSAGE" if action.action_type == 'send_dm' else ("PUBLIC REPLY" if action.action_type == 'reply_comment' else "ACTION")

            act_data = {
                "action_type": action.action_type,
                "action_label": act_label,
                "isPrimary": is_primary,
                "is_placeholder": False,
                "messages": action.messages or [],
                "dm_format": action.dm_format or 'text',
                "parent_event": action.parent_event or None,
                "loop_target_id": action.loop_target_id or None,
            }

            if action.generic_template_payload and action.generic_template_payload.get('elements'):
                act_data["generic_template_elements_json"] = json.dumps(action.generic_template_payload.get('elements', []))
            if action.button_template_payload and action.button_template_payload.get('buttons'):
                act_data["button_template_buttons_json"] = json.dumps(action.button_template_payload.get('buttons', []))
            if action.quick_reply_payload and action.quick_reply_payload.get('quick_replies'):
                act_data["quick_replies_titles"] = [qr.get('title') for qr in action.quick_reply_payload.get('quick_replies', []) if qr.get('title')]
            if action.show_profile_payload:
                act_data.update(action.show_profile_payload)
            if action.check_follow_payload:
                act_data.update(action.check_follow_payload)
            if action.attachment_payload:
                act_data["attachments"] = action.attachment_payload

            nodes.append({
                "id": a_id,
                "type": "action",
                "position": {"x": 440 if is_share and not action.parent_event else (800 + (i * 360 if action.parent_event else 0)), "y": pos_y},
                "ruleType": rule_type,
                "data": act_data
            })
            created_action_nodes.append((a_id, action))

            # Find source node and label
            if action.parent_event:
                # Find parent action
                found_parent = False
                for prev_id, prev_action in created_action_nodes[:-1]:
                    btn_title = find_button_title_in_action(prev_action, action.parent_event)
                    if btn_title:
                        edges.append({
                            "id": f"edge-reply-{prev_id}-{a_id}",
                            "source": prev_id,
                            "target": a_id,
                            "label": btn_title or action.parent_event
                        })
                        found_parent = True
                        break
                if not found_parent:
                    edges.append({
                        "id": f"edge-{rule.id}-act-{i}",
                        "source": parent_id,
                        "target": a_id,
                        "label": action.parent_event
                    })
            else:
                edges.append({
                    "id": f"edge-{rule.id}-act-{i}",
                    "source": parent_id,
                    "target": a_id
                })

            # If this is a loop_back action, also append the loop return wire
            if action.dm_format == 'loop_back' and action.loop_target_id:
                edges.append({
                    "id": f"edge-loop-{a_id}-{action.loop_target_id}",
                    "source": a_id,
                    "target": action.loop_target_id,
                    "label": "🔄 Loop Back",
                    "style": {"strokeDasharray": "5,5", "stroke": "#8b5cf6"}
                })

    constructed = {"nodes": nodes, "edges": edges}
    rule.visual_data = constructed
    try:
        rule.save(update_fields=['visual_data'])
    except Exception:
        pass
    return constructed


class AutomationListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        try:
            user.refresh_from_db(fields=['active_instagram_account'])
        except Exception:
            pass
        account = user.active_instagram_account
        if not account:
            account = InstagramAccount.objects.filter(
                user=user, is_active=True).first()

        if not account:
            return Response([], status=200)

        rules = AutomationRule.objects.filter(
            seller=account).prefetch_related('actions')

        data = []
        for rule in rules:
            actions_data = []
            for action in rule.actions.all():
                actions_data.append({
                    "action_type": action.action_type,
                    "dm_format": action.dm_format,
                    "messages": action.messages,
                    "parent_event": action.parent_event,
                    "loop_target_id": action.loop_target_id,
                    "quick_replies": action.quick_reply_payload.get("quick_replies", []) if action.quick_reply_payload else [],
                    "buttons": action.button_template_payload.get("buttons", []) if action.button_template_payload else [],
                    "elements": action.generic_template_payload.get("elements", []) if action.generic_template_payload else [],
                })

            execution_count = AutomationExecution.objects.filter(
                rule=rule, status='success').count()

            followers_gained = AutomationFollowerGain.objects.filter(rule=rule).count()

            data.append({
                "id": str(rule.id),
                "name": rule.name,
                "rule_type": rule.rule_type,
                "trigger_event": rule.trigger_event or "dm_event",
                "status": rule.status,
                "count": str(execution_count),
                "followers_gained": followers_gained,
                "keywords": rule.condition_keywords or [],
                "target_mode": rule.target_mode,
                "target_media_ids": rule.target_media_ids or [],
                "actions": actions_data,
                "visual_data": build_visual_data_from_rule(rule),
                "created_at": rule.created_at.isoformat(),
                "updated_at": rule.updated_at.isoformat(),
                "start_at": rule.start_at.isoformat() if rule.start_at else None,
                "end_at": rule.end_at.isoformat() if rule.end_at else None,
            })

        return Response(data)

    @transaction.atomic
    def post(self, request):
        user = request.user
        try:
            user.refresh_from_db(fields=['active_instagram_account'])
        except Exception:
            pass
        account = user.active_instagram_account
        if not account:
            account = InstagramAccount.objects.filter(
                user=user, is_active=True).first()

        if not account:
            return Response({"error": "No active Instagram account found"}, status=400)

        body = request.data
        rule_id = body.get("id")
        name = body.get("name", "Unnamed Automation")
        status = body.get("status", "draft")  # 'active' or 'draft'
        nodes = body.get("nodes", [])
        edges = body.get("edges", [])

        # Find or create the AutomationRule
        if rule_id:
            # Check if it's a numerical ID or UUID, handle accordingly
            try:
                rule = AutomationRule.objects.select_for_update().get(id=rule_id, seller=account)
            except (AutomationRule.DoesNotExist, ValueError):
                # If editing a local-only node before save, create a new one
                rule = AutomationRule(seller=account)
        else:
            rule = AutomationRule(seller=account)

        rule.name = name
        rule.status = "active" if status == "active" else "draft"
        rule.visual_data = {"nodes": nodes, "edges": edges}

        # Extract trigger node details
        trigger_node = next(
            (n for n in nodes if n.get('type') == 'trigger'), None)
        if trigger_node:
            rule.rule_type = trigger_node.get('ruleType', 'comment_automation')
            t_data = trigger_node.get('data', {})
            rule.target_mode = t_data.get('target_mode', 'every')
            rule.target_media_ids = t_data.get('media_ids', [])
            rule.target_media_type = t_data.get('media_type', '')
            rule.start_at = t_data.get('start_at') or None
            rule.end_at = t_data.get('end_at') or None

        # Extract condition node details
        condition_node = next(
            (n for n in nodes if n.get('type') == 'condition'), None)
        if condition_node:
            c_data = condition_node.get('data', {})
            match_type = c_data.get('match_type')
            if not match_type:
                # Fallback: if keywords exist, default to contains. Otherwise default to any.
                has_keywords = bool(c_data.get('keywords')
                                    or c_data.get('keywords_equals'))
                match_type = 'contains' if has_keywords else 'any'

            rule.condition_match_type = match_type
            if match_type == 'any':
                rule.condition_keywords = []
            elif match_type == 'equals':
                rule.condition_keywords = c_data.get('keywords_equals', [])
            else:
                rule.condition_keywords = c_data.get('keywords', [])
            
            rule.follower_gate_enabled = c_data.get('follower_gate', False)
            rule.follower_gate_messages = c_data.get('follower_gate_messages', [])
        else:
            # Default to match any if no condition node exists
            rule.condition_match_type = 'any'
            rule.condition_keywords = []
            rule.follower_gate_enabled = False
            rule.follower_gate_messages = []

        rule.save()

        # Rebuild AutomationActions
        rule.actions.all().delete()
        action_nodes = [n for n in nodes if n.get('type') == 'action' and not n.get(
            'data', {}).get('is_placeholder', False) and not n.get('data', {}).get('is_cf_fork', False)]

        # Find which action nodes are child nodes (connected from other action nodes or cf_fork)
        action_node_ids = {n.get('id') for n in action_nodes}
        child_node_ids = set()
        for e in edges:
            src = e.get('source')
            tgt = e.get('target')
            if tgt in action_node_ids:
                if src in action_node_ids and not is_loop_return_edge(e):
                    child_node_ids.add(tgt)
                elif '-cf-fork' in str(src) or 'fork' in str(src):
                    child_node_ids.add(tgt)

        # Sort actions: Root actions first (order=0, 1...), then child actions by y-position
        action_nodes.sort(key=lambda n: (1 if n.get('id') in child_node_ids else 0, n.get('position', {}).get('y', 0)))

        for idx, node in enumerate(action_nodes):
            a_data = node.get('data', {})
            action_type = a_data.get('action_type', 'send_dm')
            dm_format = a_data.get('dm_format', 'text')
            messages = a_data.get('messages', [])

            if not messages or not isinstance(messages, list):
                messages = [a_data.get('text', 'Thanks for your interest!')]

            # If this is a root action (not a child of any action), force parent_event=None
            parent_event = a_data.get('parent_event')
            if node.get('id') not in child_node_ids:
                parent_event = None
                if isinstance(node.get('data'), dict):
                    node['data']['parent_event'] = None

            # Detect cf_branch
            cf_branch = a_data.get('cf_branch')
            if not cf_branch:
                if a_data.get('is_cf_following') or 'following' in str(node.get('id')):
                    cf_branch = 'following'
                elif a_data.get('is_cf_not_following') or 'not-following' in str(node.get('id')):
                    cf_branch = 'not_following'

            action = AutomationAction(
                rule=rule,
                order=idx,
                action_type=action_type,
                dm_format=dm_format,
                messages=messages,
                message_mode=a_data.get('message_mode', 'random'),
                parent_event=parent_event,
                loop_target_id=a_data.get('loop_target_id')
            )
            if cf_branch:
                action.check_follow_payload = {"cf_branch": cf_branch}

            # Store format-specific payloads
            if dm_format == 'quick_reply':
                qr_text = a_data.get('quick_reply_text', '')
                qr_titles = a_data.get('quick_replies_titles', [])
                if isinstance(qr_titles, str):
                    qr_titles = [t.strip()
                                 for t in qr_titles.split(',') if t.strip()]

                quick_replies = []
                for title in qr_titles:
                    quick_replies.append({
                        "content_type": "text",
                        "title": title[:20],
                        "payload": f"QR_{title[:20].upper().replace(' ', '_')}"
                    })
                action.quick_reply_payload = {"quick_replies": quick_replies}
                if qr_text:
                    action.messages = [qr_text]

            elif dm_format == 'button_template':
                btn_text = a_data.get('button_template_text', '')
                btn_json = a_data.get('button_template_buttons_json', '[]')
                buttons = []
                if isinstance(btn_json, str) and btn_json.strip():
                    try:
                        import json
                        buttons = json.loads(btn_json)
                    except Exception:
                        pass
                elif isinstance(btn_json, list):
                    buttons = btn_json

                # Convert 'product' buttons to 'web_url' for Meta Graph API compliance
                cleaned_buttons = []
                for btn in buttons:
                    btn_copy = dict(btn)
                    if btn_copy.get("type") == "product":
                        btn_copy["type"] = "web_url"
                    cleaned_buttons.append(btn_copy)

                action.button_template_payload = {"buttons": cleaned_buttons}
                if btn_text:
                    action.messages = [btn_text]

            elif dm_format == 'generic_template':
                elems_json = a_data.get('generic_template_elements_json', '[]')
                elements = []
                if isinstance(elems_json, str) and elems_json.strip():
                    try:
                        import json
                        elements = json.loads(elems_json)
                    except Exception:
                        pass
                elif isinstance(elems_json, list):
                    elements = elems_json

                # Convert 'product' buttons to 'web_url' for Meta Graph API compliance in carousel elements
                cleaned_elements = []
                for elem in elements:
                    elem_copy = dict(elem)
                    if "buttons" in elem_copy:
                        card_btns = []
                        for btn in elem_copy["buttons"]:
                            btn_copy = dict(btn)
                            if btn_copy.get("type") == "product":
                                btn_copy["type"] = "web_url"
                            card_btns.append(btn_copy)
                        elem_copy["buttons"] = card_btns
                    cleaned_elements.append(elem_copy)

                action.generic_template_payload = {
                    "elements": cleaned_elements}

            elif dm_format == 'attachment':
                attachments_raw = a_data.get('attachments', [])
                items = []
                if isinstance(attachments_raw, str) and attachments_raw.strip():
                    try:
                        import json
                        items = json.loads(attachments_raw)
                    except Exception:
                        pass
                elif isinstance(attachments_raw, list):
                    items = attachments_raw

                # Convert to structured attachment objects
                structured_attachments = []
                for item in items:
                    if isinstance(item, str):
                        # Backward compatibility: convert flat string URLs to image attachments
                        structured_attachments.append({
                            "type": "image",
                            "url": item
                        })
                    elif isinstance(item, dict):
                        structured_attachments.append({
                            "type": item.get("type", "image"),
                            "url": item.get("url"),
                            "media_id": item.get("media_id"),
                            "sticker_id": item.get("sticker_id")
                        })
                action.attachment_payload = structured_attachments

            elif dm_format == 'show_profile':
                profile_url = a_data.get('profile_url', '')
                profile_msg = a_data.get('profile_message_text', '')
                profile_btn = a_data.get('profile_button_text', '')
                action.show_profile_payload = {
                    "profile_url": profile_url,
                    "profile_message_text": profile_msg,
                    "profile_button_text": profile_btn
                }
                if profile_msg:
                    action.messages = [profile_msg]

            elif dm_format == 'check_follow':
                action.check_follow_payload = {
                    "following_format": a_data.get('following_format', 'text'),
                    "following_text": a_data.get('following_text', ''),
                    "following_button_text": a_data.get('following_button_text', ''),
                    "following_buttons_json": a_data.get('following_buttons_json', ''),
                    "following_profile_url": a_data.get('following_profile_url', ''),

                    "not_following_format": a_data.get('not_following_format', 'button_template'),
                    "not_following_text": a_data.get('not_following_text', ''),
                    "not_following_button_text": a_data.get('not_following_button_text', ''),
                    "not_following_buttons_json": a_data.get('not_following_buttons_json', ''),
                    "not_following_profile_url": a_data.get('not_following_profile_url', '')
                }

            elif dm_format == 'loop_back':
                action.loop_target_id = a_data.get('loop_target_id')
                if not action.messages or action.messages == ['Thanks for your interest!']:
                    action.messages = ["Loop Back"]

            action.save()

        # Rebuild GiveawayConfig if present
        giveaway_node = next(
            (n for n in nodes if n.get('type') == 'giveaway_config'), None)
        if giveaway_node:
            g_data = giveaway_node.get('data', {})
            g_config, _ = GiveawayConfig.objects.get_or_create(rule=rule)
            g_config.selection_method = g_data.get(
                'selection_method', 'random')
            g_config.winner_count = g_data.get('winner_count', 1)
            g_config.finalize_at = g_data.get('finalize_at')
            g_config.save()

            # Rebuild rewards
            g_config.reward_pool.all().delete()
            reward_nodes = [n for n in nodes if n.get('type') == 'reward']
            for r_node in reward_nodes:
                r_data = r_node.get('data', {})
                GiveawayReward.objects.create(
                    giveaway=g_config,
                    reward_id=r_node.get('id'),
                    reward_type=r_data.get('reward_type', 'discount'),
                    value=r_data.get('value', ''),
                    quantity=r_data.get('quantity', 1),
                    remaining=r_data.get('remaining', 1)
                )

        return Response({
            "success": True,
            "id": rule.id,
            "name": rule.name,
            "status": "active" if rule.status == "active" else "disabled"
        })


class AutomationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        user = request.user
        account = user.active_instagram_account or InstagramAccount.objects.filter(
            user=user, is_active=True).first()
        if not account:
            return Response({"error": "No active Instagram account"}, status=400)

        rule = get_object_or_404(AutomationRule, id=pk, seller=account)
        actions_data = []
        for action in rule.actions.all():
            actions_data.append({
                "action_type": action.action_type,
                "dm_format": action.dm_format,
                "messages": action.messages,
                "parent_event": action.parent_event,
                "loop_target_id": action.loop_target_id,
                "quick_replies": action.quick_reply_payload.get("quick_replies", []) if action.quick_reply_payload else [],
                "buttons": action.button_template_payload.get("buttons", []) if action.button_template_payload else [],
                "elements": action.generic_template_payload.get("elements", []) if action.generic_template_payload else [],
            })

        execution_count = AutomationExecution.objects.filter(
            rule=rule, status='success').count()

        return Response({
            "id": str(rule.id),
            "name": rule.name,
            "rule_type": rule.rule_type,
            "trigger_event": rule.trigger_event or "dm_event",
            "status": "active" if rule.status == "active" else "disabled",
            "count": str(execution_count),
            "keywords": rule.condition_keywords or [],
            "target_mode": rule.target_mode,
            "target_media_ids": rule.target_media_ids or [],
            "actions": actions_data,
            "visual_data": build_visual_data_from_rule(rule),
            "created_at": rule.created_at.isoformat(),
            "updated_at": rule.updated_at.isoformat(),
            "start_at": rule.start_at.isoformat() if rule.start_at else None,
            "end_at": rule.end_at.isoformat() if rule.end_at else None,
        })

    def delete(self, request, pk):
        user = request.user
        account = user.active_instagram_account or InstagramAccount.objects.filter(
            user=user, is_active=True).first()
        if not account:
            return Response({"error": "No active Instagram account"}, status=400)

        rule = get_object_or_404(AutomationRule, id=pk, seller=account)
        rule.delete()
        return Response({"success": True})


class AutomationToggleView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        account = user.active_instagram_account or InstagramAccount.objects.filter(
            user=user, is_active=True).first()
        if not account:
            return Response({"error": "No active Instagram account"}, status=400)

        rule = get_object_or_404(AutomationRule, id=pk, seller=account)
        is_enabled = request.data.get("isEnabled", False)

        rule.status = "active" if is_enabled else "draft"
        rule.save(update_fields=["status"])

        return Response({
            "success": True,
            "id": rule.id,
            "status": "active" if rule.status == "active" else "disabled"
        })


def cron_trigger(request):
    fake_redis_task.delay()

    return JsonResponse({
        "status": "Django working",
        "message": "Task sent to Celery via Redis queue"
    })


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED POST VIEWSET & SERIALIZER
# ─────────────────────────────────────────────────────────────────────────────

from rest_framework import serializers, viewsets, status
from rest_framework.decorators import action
from django.utils import timezone
from django.db import models
from django.db.models import Q
from .models import ScheduledPost
from .tasks import publish_scheduled_post_task


class ScheduledPostSerializer(serializers.ModelSerializer):
    account_username = serializers.CharField(source='seller.username', read_only=True)
    post_type_label = serializers.CharField(source='get_post_type_display', read_only=True)

    class Meta:
        model = ScheduledPost
        fields = [
            'id', 'post_type', 'post_type_label', 'media_url', 'cover_url',
            'carousel_urls', 'caption', 'share_to_feed', 'scheduled_at',
            'status', 'container_id', 'instagram_media_id', 'instagram_permalink',
            'error_message', 'product', 'published_at', 'created_at', 'updated_at',
            'account_username'
        ]
        read_only_fields = [
            'id', 'container_id', 'instagram_media_id',
            'instagram_permalink', 'error_message', 'published_at',
            'created_at', 'updated_at', 'account_username', 'post_type_label'
        ]


class ScheduledPostViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Instagram Scheduled Posts with Publish-Now support.
    """
    serializer_class = ScheduledPostSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = ScheduledPost.objects.filter(
            models.Q(user=user) | models.Q(seller__user=user)
        )
        
        # Filter by status if query param provided
        status_param = self.request.query_params.get('status')
        if status_param and status_param != 'ALL':
            queryset = queryset.filter(status=status_param.upper())

        # Filter by post_type
        type_param = self.request.query_params.get('type')
        if type_param and type_param != 'ALL':
            queryset = queryset.filter(post_type=type_param.upper())

        return queryset.order_by('-scheduled_at', '-created_at')

    def perform_create(self, serializer):
        user = self.request.user
        active_ig = getattr(user, 'active_instagram_account', None) or user.instagram_accounts.filter(is_active=True).first()
        if not active_ig:
            raise serializers.ValidationError({"error": "No connected Instagram account found. Please connect an account first."})

        publish_now = self.request.data.get('publish_now', False)
        scheduled_at = serializer.validated_data.get('scheduled_at')

        if publish_now or not scheduled_at:
            post = serializer.save(
                user=user,
                seller=active_ig,
                scheduled_at=timezone.now(),
                status='PROCESSING'
            )
            # Execute asynchronously or directly
            try:
                publish_scheduled_post_task.delay(post.id)
            except Exception:
                publish_scheduled_post_task(post.id)
        else:
            post = serializer.save(
                user=user,
                seller=active_ig,
                status='SCHEDULED'
            )
            # If celery supports ETA, queue it; otherwise periodic worker handles it
            try:
                publish_scheduled_post_task.apply_async(args=[post.id], eta=post.scheduled_at)
            except Exception:
                pass

        # Broadcast real-time creation event over WebSocket
        try:
            from .publishing import broadcast_scheduled_post_update
            broadcast_scheduled_post_update(post, "created")
        except Exception:
            pass

    def perform_destroy(self, instance):
        post_id = instance.id
        user_id = instance.user_id
        seller_id = instance.seller_id
        instance.delete()

        # Broadcast deletion over WebSocket
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            cl = get_channel_layer()
            if cl:
                payload = {
                    "event_type": "scheduled_post_update",
                    "action": "deleted",
                    "post_id": post_id
                }
                if user_id:
                    async_to_sync(cl.group_send)(f"user_{user_id}", {"type": "chat_message", "payload": payload})
                if seller_id:
                    async_to_sync(cl.group_send)(f"instagram_{seller_id}", {"type": "chat_message", "payload": payload})
        except Exception:
            pass

    @action(detail=True, methods=['post'], url_path='publish-now')
    def publish_now(self, request, pk=None):
        post = self.get_object()
        post.status = 'PROCESSING'
        post.scheduled_at = timezone.now()
        post.save(update_fields=['status', 'scheduled_at'])

        try:
            from .publishing import broadcast_scheduled_post_update
            broadcast_scheduled_post_update(post, "processing")
        except Exception:
            pass

        try:
            publish_scheduled_post_task.delay(post.id)
        except Exception:
            publish_scheduled_post_task(post.id)

        post.refresh_from_db()
        serializer = self.get_serializer(post)
        return Response({
            "status": "success",
            "message": "Publish triggered successfully",
            "post": serializer.data
        }, status=status.HTTP_200_OK)

