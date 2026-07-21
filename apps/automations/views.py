from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.shortcuts import get_object_or_404
from apps.accounts.models import InstagramAccount
from apps.automations.models import AutomationRule, AutomationAction, GiveawayConfig, GiveawayReward, AutomationExecution
from apps.crm.tasks import fake_redis_task
from django.http import JsonResponse


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
                    "quick_replies": action.quick_reply_payload.get("quick_replies", []) if action.quick_reply_payload else [],
                    "buttons": action.button_template_payload.get("buttons", []) if action.button_template_payload else [],
                    "elements": action.generic_template_payload.get("elements", []) if action.generic_template_payload else [],
                })

            execution_count = AutomationExecution.objects.filter(
                rule=rule, status='success').count()

            data.append({
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
                "visual_data": rule.visual_data or {},
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
            'data', {}).get('is_placeholder', False)]

        # Sort actions by y-position to preserve execution order
        action_nodes.sort(key=lambda n: n.get('position', {}).get('y', 0))

        for idx, node in enumerate(action_nodes):
            a_data = node.get('data', {})
            action_type = a_data.get('action_type', 'send_dm')
            dm_format = a_data.get('dm_format', 'text')
            messages = a_data.get('messages', [])

            if not messages or not isinstance(messages, list):
                messages = [a_data.get('text', 'Thanks for your interest!')]

            action = AutomationAction(
                rule=rule,
                order=idx,
                action_type=action_type,
                dm_format=dm_format,
                messages=messages,
                message_mode=a_data.get('message_mode', 'random'),
                parent_event=a_data.get('parent_event')
            )

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
            "visual_data": rule.visual_data or {},
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
