import os
import sys
import django
import json

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from apps.accounts.models import User, InstagramAccount
from apps.automations.models import AutomationRule, AutomationAction, AutomationExecution
from apps.crm.models import Customer, CustomerInteraction
from apps.automations.engine import execute_automation

def setup_my_muscles_factory_automations():
    print("Setting up automations for muhammedanasmp2001@gmail.com (my_muscles_factory)...")
    
    user = User.objects.filter(email="muhammedanasmp2001@gmail.com").first()
    if not user:
        print("User muhammedanasmp2001@gmail.com not found!")
        return
    
    account = InstagramAccount.objects.filter(id=12, user=user).first()
    if not account:
        account = InstagramAccount.objects.filter(username="my_muscles_factory").first()
    if not account:
        print("Instagram account my_muscles_factory not found!")
        return
    
    user.active_instagram_account = account
    user.save()
    print(f"Found seller account: {account.username} (ID: {account.id}) for user {user.email}")

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Price & Plans Inquiry Automation with Back Loop
    # ─────────────────────────────────────────────────────────────────────────
    rule1_name = "Price & Plans Inquiry with Back Loop"
    rule1, _ = AutomationRule.objects.get_or_create(
        seller=account,
        name=rule1_name,
        defaults={
            'rule_type': 'comment_automation',
            'status': 'active',
            'target_mode': 'every',
            'condition_match_type': 'contains',
            'condition_keywords': ['price', 'plan', 'cost', 'workout', 'membership']
        }
    )
    rule1.rule_type = 'comment_automation'
    rule1.status = 'active'
    rule1.target_mode = 'every'
    rule1.condition_match_type = 'contains'
    rule1.condition_keywords = ['price', 'plan', 'cost', 'workout', 'membership']

    # Visual Layout
    root_node_id = f"node-a-{rule1.id}-root"
    plans_node_id = f"node-a-{rule1.id}-plans"
    diet_node_id = f"node-a-{rule1.id}-diet"
    loop_node_id = f"node-a-{rule1.id}-loop"

    nodes1 = [
        {
            "id": f"node-t-{rule1.id}",
            "type": "trigger",
            "position": {"x": 80, "y": 150},
            "ruleType": "comment_automation",
            "data": {"target_mode": "every", "media_ids": [], "media_type": "post"}
        },
        {
            "id": f"node-c-{rule1.id}",
            "type": "condition",
            "position": {"x": 440, "y": 150},
            "ruleType": "comment_automation",
            "data": {"match_type": "contains", "keywords": ['price', 'plan', 'cost', 'workout', 'membership']}
        },
        {
            "id": f"node-a-{rule1.id}-pub",
            "type": "action",
            "position": {"x": 800, "y": 80},
            "ruleType": "comment_automation",
            "data": {
                "action_type": "reply_comment",
                "action_label": "PUBLIC REPLY",
                "isPrimary": False,
                "is_placeholder": False,
                "messages": ["Thanks for reaching out! Check your DMs for our full plans & pricing! 💪"]
            }
        },
        {
            "id": root_node_id,
            "type": "action",
            "position": {"x": 800, "y": 400},
            "ruleType": "comment_automation",
            "data": {
                "action_type": "send_dm",
                "action_label": "DIRECT MESSAGE",
                "isPrimary": True,
                "is_placeholder": False,
                "dm_format": "button_template",
                "button_template_text": "Welcome to My Muscles Factory! Choose an option to explore our fitness programs:",
                "messages": ["Welcome to My Muscles Factory! Choose an option to explore our fitness programs:"],
                "button_template_buttons_json": json.dumps([
                    {"type": "postback", "title": "💪 Workout Plans", "payload": "VIEW_WORKOUT_PLANS"},
                    {"type": "postback", "title": "🥗 Diet Guide", "payload": "VIEW_DIET_GUIDE"},
                    {"type": "web_url", "title": "👤 Visit Profile", "url": f"https://instagram.com/{account.username}"}
                ])
            }
        },
        {
            "id": plans_node_id,
            "type": "action",
            "position": {"x": 1160, "y": 400},
            "ruleType": "comment_automation",
            "data": {
                "action_type": "send_dm",
                "action_label": "DIRECT MESSAGE",
                "parent_event": "VIEW_WORKOUT_PLANS",
                "is_placeholder": False,
                "dm_format": "button_template",
                "button_template_text": "🏋️ Choose your workout tier:\n- Beginner (3 days/wk)\n- Hypertrophy (5 days/wk)\n- Elite Prep (6 days/wk)",
                "messages": ["🏋️ Choose your workout tier:\n- Beginner (3 days/wk)\n- Hypertrophy (5 days/wk)\n- Elite Prep (6 days/wk)"],
                "button_template_buttons_json": json.dumps([
                    {"type": "postback", "title": "🔥 Beginner Plan", "payload": "PLAN_BEGINNER"},
                    {"type": "postback", "title": "🔄 Main Menu", "payload": "LOOP_TO_MAIN_MENU"}
                ])
            }
        },
        {
            "id": loop_node_id,
            "type": "action",
            "position": {"x": 1520, "y": 400},
            "ruleType": "comment_automation",
            "data": {
                "action_type": "send_dm",
                "action_label": "DIRECT MESSAGE",
                "parent_event": "LOOP_TO_MAIN_MENU",
                "is_placeholder": False,
                "dm_format": "loop_back",
                "loop_target_id": root_node_id,
                "messages": ["Loop Back"]
            }
        }
    ]

    edges1 = [
        {"id": f"e1-{rule1.id}-tc", "source": f"node-t-{rule1.id}", "target": f"node-c-{rule1.id}"},
        {"id": f"e1-{rule1.id}-cp", "source": f"node-c-{rule1.id}", "target": f"node-a-{rule1.id}-pub"},
        {"id": f"e1-{rule1.id}-cdm", "source": f"node-c-{rule1.id}", "target": root_node_id},
        {"id": f"e1-{rule1.id}-np", "source": root_node_id, "target": plans_node_id, "label": "💪 Workout Plans"},
        {"id": f"e1-{rule1.id}-nl", "source": plans_node_id, "target": loop_node_id, "label": "🔄 Main Menu"},
        {"id": f"edge-loop-{loop_node_id}-{root_node_id}", "source": loop_node_id, "target": root_node_id, "label": "🔄 Loop Back", "style": {"strokeDasharray": "5,5", "stroke": "#8b5cf6"}}
    ]

    rule1.visual_data = {"nodes": nodes1, "edges": edges1}
    rule1.save()

    # Rebuild Actions for Rule 1
    rule1.actions.all().delete()
    AutomationAction.objects.create(
        rule=rule1,
        order=0,
        action_type="reply_comment",
        messages=["Thanks for reaching out! Check your DMs for our full plans & pricing! 💪"]
    )
    AutomationAction.objects.create(
        rule=rule1,
        order=1,
        action_type="send_dm",
        dm_format="button_template",
        messages=["Welcome to My Muscles Factory! Choose an option to explore our fitness programs:"],
        button_template_payload={
            "buttons": [
                {"type": "postback", "title": "💪 Workout Plans", "payload": "VIEW_WORKOUT_PLANS"},
                {"type": "postback", "title": "🥗 Diet Guide", "payload": "VIEW_DIET_GUIDE"},
                {"type": "web_url", "title": "👤 Visit Profile", "url": f"https://instagram.com/{account.username}"}
            ]
        }
    )
    AutomationAction.objects.create(
        rule=rule1,
        order=2,
        action_type="send_dm",
        dm_format="button_template",
        parent_event="VIEW_WORKOUT_PLANS",
        messages=["🏋️ Choose your workout tier:\n- Beginner (3 days/wk)\n- Hypertrophy (5 days/wk)\n- Elite Prep (6 days/wk)"],
        button_template_payload={
            "buttons": [
                {"type": "postback", "title": "🔥 Beginner Plan", "payload": "PLAN_BEGINNER"},
                {"type": "postback", "title": "🔄 Main Menu", "payload": "LOOP_TO_MAIN_MENU"}
            ]
        }
    )
    AutomationAction.objects.create(
        rule=rule1,
        order=3,
        action_type="send_dm",
        dm_format="loop_back",
        parent_event="LOOP_TO_MAIN_MENU",
        loop_target_id=root_node_id,
        messages=["Loop Back"]
    )
    print(f"[OK] Created/Updated Rule 1: '{rule1.name}' (ID: {rule1.id}) with Back Loop")

    # ─────────────────────────────────────────────────────────────────────────
    # 2. VIP Follower Gate Automation with Back Loop
    # ─────────────────────────────────────────────────────────────────────────
    rule2_name = "VIP Follower Gate & Deal with Back Loop"
    rule2, _ = AutomationRule.objects.get_or_create(
        seller=account,
        name=rule2_name,
        defaults={
            'rule_type': 'dm_automation',
            'status': 'active',
            'target_mode': 'every',
            'condition_match_type': 'contains',
            'condition_keywords': ['vip', 'giveaway', 'discount', 'free', 'offer']
        }
    )
    rule2.rule_type = 'dm_automation'
    rule2.status = 'active'
    rule2.target_mode = 'every'
    rule2.condition_match_type = 'contains'
    rule2.condition_keywords = ['vip', 'giveaway', 'discount', 'free', 'offer']

    gate_node_id = f"node-a-{rule2.id}-gate"
    gate_loop_node_id = f"node-a-{rule2.id}-gate-loop"

    nodes2 = [
        {
            "id": f"node-t-{rule2.id}",
            "type": "trigger",
            "position": {"x": 80, "y": 150},
            "ruleType": "dm_automation",
            "data": {"target_mode": "every"}
        },
        {
            "id": f"node-c-{rule2.id}",
            "type": "condition",
            "position": {"x": 440, "y": 150},
            "ruleType": "dm_automation",
            "data": {"match_type": "contains", "keywords": ['vip', 'giveaway', 'discount', 'free', 'offer']}
        },
        {
            "id": gate_node_id,
            "type": "action",
            "position": {"x": 800, "y": 150},
            "ruleType": "dm_automation",
            "data": {
                "action_type": "send_dm",
                "dm_format": "check_follow",
                "following_format": "button_template",
                "following_text": "🎉 VIP Unlocked! Welcome to My Muscles Factory inner circle. Here is your 50% discount voucher:",
                "following_buttons_json": json.dumps([
                    {"type": "postback", "title": "🎁 Claim 50% Off", "payload": "CLAIM_VIP_DISCOUNT"}
                ]),
                "not_following_format": "button_template",
                "not_following_text": "🔒 You must follow @my_muscles_factory to unlock exclusive VIP fitness deals!",
                "not_following_buttons_json": json.dumps([
                    {"type": "web_url", "title": "👉 Follow Us", "url": f"https://instagram.com/{account.username}"},
                    {"type": "postback", "title": "🔄 I Followed (Check)", "payload": "RECHECK_VIP_GATE"}
                ]),
                "not_following_profile_url": f"https://instagram.com/{account.username}"
            }
        },
        {
            "id": gate_loop_node_id,
            "type": "action",
            "position": {"x": 1160, "y": 150},
            "ruleType": "dm_automation",
            "data": {
                "action_type": "send_dm",
                "dm_format": "loop_back",
                "parent_event": "RECHECK_VIP_GATE",
                "loop_target_id": gate_node_id,
                "messages": ["Loop Back"]
            }
        }
    ]

    edges2 = [
        {"id": f"e2-{rule2.id}-tc", "source": f"node-t-{rule2.id}", "target": f"node-c-{rule2.id}"},
        {"id": f"e2-{rule2.id}-cg", "source": f"node-c-{rule2.id}", "target": gate_node_id},
        {"id": f"e2-{rule2.id}-gl", "source": gate_node_id, "target": gate_loop_node_id, "label": "🔄 I Followed (Check)"},
        {"id": f"edge-loop-{gate_loop_node_id}-{gate_node_id}", "source": gate_loop_node_id, "target": gate_node_id, "label": "🔄 Loop Back", "style": {"strokeDasharray": "5,5", "stroke": "#8b5cf6"}}
    ]

    rule2.visual_data = {"nodes": nodes2, "edges": edges2}
    rule2.save()

    rule2.actions.all().delete()
    AutomationAction.objects.create(
        rule=rule2,
        order=0,
        action_type="send_dm",
        dm_format="check_follow",
        check_follow_payload={
            "following_format": "button_template",
            "following_text": "🎉 VIP Unlocked! Welcome to My Muscles Factory inner circle. Here is your 50% discount voucher:",
            "following_buttons_json": json.dumps([
                {"type": "postback", "title": "🎁 Claim 50% Off", "payload": "CLAIM_VIP_DISCOUNT"}
            ]),
            "not_following_format": "button_template",
            "not_following_text": "🔒 You must follow @my_muscles_factory to unlock exclusive VIP fitness deals!",
            "not_following_buttons_json": json.dumps([
                {"type": "web_url", "title": "👉 Follow Us", "url": f"https://instagram.com/{account.username}"},
                {"type": "postback", "title": "🔄 I Followed (Check)", "payload": "RECHECK_VIP_GATE"}
            ]),
            "not_following_profile_url": f"https://instagram.com/{account.username}"
        },
        messages=["VIP Follower Check"]
    )
    AutomationAction.objects.create(
        rule=rule2,
        order=1,
        action_type="send_dm",
        dm_format="loop_back",
        parent_event="RECHECK_VIP_GATE",
        loop_target_id=gate_node_id,
        messages=["Loop Back"]
    )
    print(f"[OK] Created/Updated Rule 2: '{rule2.name}' (ID: {rule2.id}) with Back Loop")

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Story Reply Automation with Back Loop
    # ─────────────────────────────────────────────────────────────────────────
    rule3_name = "Story Reply Assistant with Back Loop"
    rule3, _ = AutomationRule.objects.get_or_create(
        seller=account,
        name=rule3_name,
        defaults={
            'rule_type': 'story_automation',
            'status': 'active',
            'target_mode': 'every',
            'condition_match_type': 'any'
        }
    )
    rule3.rule_type = 'story_automation'
    rule3.status = 'active'
    rule3.target_mode = 'every'
    rule3.condition_match_type = 'any'

    story_root_id = f"node-a-{rule3.id}-story-root"
    story_loop_id = f"node-a-{rule3.id}-story-loop"

    nodes3 = [
        {
            "id": f"node-t-{rule3.id}",
            "type": "trigger",
            "position": {"x": 80, "y": 150},
            "ruleType": "story_automation",
            "data": {"target_mode": "every"}
        },
        {
            "id": story_root_id,
            "type": "action",
            "position": {"x": 440, "y": 150},
            "ruleType": "story_automation",
            "data": {
                "action_type": "send_dm",
                "dm_format": "button_template",
                "button_template_text": "Thanks for reacting to our story! 🔥 How can we assist your workout journey?",
                "messages": ["Thanks for reacting to our story! 🔥 How can we assist your workout journey?"],
                "button_template_buttons_json": json.dumps([
                    {"type": "postback", "title": "🏋️ Free Consultation", "payload": "STORY_CONSULT"},
                    {"type": "postback", "title": "🔄 Start Over", "payload": "STORY_LOOP_RESTART"}
                ]),
                "is_placeholder": False
            }
        },
        {
            "id": story_loop_id,
            "type": "action",
            "position": {"x": 800, "y": 150},
            "ruleType": "story_automation",
            "data": {
                "action_type": "send_dm",
                "dm_format": "loop_back",
                "parent_event": "STORY_LOOP_RESTART",
                "loop_target_id": story_root_id,
                "messages": ["Loop Back"],
                "is_placeholder": False
            }
        }
    ]

    edges3 = [
        {"id": f"e3-{rule3.id}-tr", "source": f"node-t-{rule3.id}", "target": story_root_id},
        {"id": f"e3-{rule3.id}-sl", "source": story_root_id, "target": story_loop_id, "label": "🔄 Start Over"},
        {"id": f"edge-loop-{story_loop_id}-{story_root_id}", "source": story_loop_id, "target": story_root_id, "label": "🔄 Loop Back", "style": {"strokeDasharray": "5,5", "stroke": "#8b5cf6"}}
    ]

    rule3.visual_data = {"nodes": nodes3, "edges": edges3}
    rule3.save()

    rule3.actions.all().delete()
    AutomationAction.objects.create(
        rule=rule3,
        order=0,
        action_type="send_dm",
        dm_format="button_template",
        messages=["Thanks for reacting to our story! 🔥 How can we assist your workout journey?"],
        button_template_payload={
            "buttons": [
                {"type": "postback", "title": "🏋️ Free Consultation", "payload": "STORY_CONSULT"},
                {"type": "postback", "title": "🔄 Start Over", "payload": "STORY_LOOP_RESTART"}
            ]
        }
    )
    AutomationAction.objects.create(
        rule=rule3,
        order=1,
        action_type="send_dm",
        dm_format="loop_back",
        parent_event="STORY_LOOP_RESTART",
        loop_target_id=story_root_id,
        messages=["Loop Back"]
    )
    print(f"[OK] Created/Updated Rule 3: '{rule3.name}' (ID: {rule3.id}) with Back Loop")

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Update Rule 131 Layout (Welcome Message Flow: SUPPORT)
    # ─────────────────────────────────────────────────────────────────────────
    rule131 = AutomationRule.objects.filter(id=131).first()
    if rule131:
        r131_t_id = "node-t-1788596911678"
        r131_c_id = "node-c-1788596911678"
        r131_root_id = "node-a-1788596911678-0"
        r131_profile_id = f"{r131_root_id}-profile-card"
        r131_fork_id = f"{r131_root_id}-cf-fork"
        r131_following_id = f"{r131_root_id}-cf-following"
        r131_not_following_id = f"{r131_root_id}-cf-not-following"

        nodes131 = [
            {
                "id": r131_t_id,
                "type": "trigger",
                "position": {"x": 80, "y": 150},
                "data": {"start_at": None, "end_at": None, "mode": "every", "media_type": None, "priority": 100, "status": "active"},
                "ruleType": "dm_automation",
                "templateId": "21"
            },
            {
                "id": r131_c_id,
                "type": "condition",
                "position": {"x": 440, "y": 150},
                "data": {"match_type": "any", "keywords": [], "follower_gate": False, "follower_gate_messages": []},
                "ruleType": "dm_automation",
                "templateId": "21"
            },
            {
                "id": r131_root_id,
                "type": "action",
                "position": {"x": 800, "y": 150},
                "data": {
                    "action_type": "send_dm",
                    "parent_event": None,
                    "is_placeholder": False,
                    "dm_format": "button_template",
                    "button_template_text": "Are you following me",
                    "button_template_buttons_json": json.dumps([
                        {"type": "web_url", "title": "Check follow", "url": f"https://instagram.com/{account.username}", "is_profile_button": True},
                        {"type": "postback", "title": "I am following", "payload": "CHECK_FOLLOW", "is_profile_button": False}
                    ]),
                    "messages": ["Are you following me"]
                },
                "ruleType": "dm_automation",
                "templateId": "21"
            },
            {
                "id": r131_profile_id,
                "type": "action",
                "position": {"x": 1160, "y": -100},
                "data": {
                    "action_type": "send_dm",
                    "dm_format": "show_profile",
                    "profile_url": f"https://instagram.com/{account.username}",
                    "profile_button_text": "Check follow",
                    "parent_event": "SHOW_PROFILE",
                    "parent_label": "Check follow",
                    "is_profile_card": True,
                    "is_placeholder": False
                }
            },
            {
                "id": r131_fork_id,
                "type": "action",
                "position": {"x": 1160, "y": 340},
                "data": {
                    "action_type": "send_dm",
                    "parent_event": "CHECK_FOLLOW",
                    "parent_label": "I am following",
                    "button_name": "I am following",
                    "is_cf_fork": True,
                    "is_placeholder": False
                }
            },
            {
                "id": r131_following_id,
                "type": "action",
                "position": {"x": 1240, "y": 180},
                "data": {
                    "action_type": "send_dm",
                    "parent_event": "CHECK_FOLLOW",
                    "cf_branch": "following",
                    "parent_label": "✅ If Following",
                    "is_cf_following": True,
                    "is_placeholder": False,
                    "dm_format": "generic_template",
                    "generic_template_elements_json": json.dumps([
                        {
                            "title": "Welcome Product",
                            "subtitle": "Premium quality slider description.",
                            "image_url": "https://loremflickr.com/400/300/cat",
                            "default_action": {"type": "web_url", "url": "https://shop.example.com"},
                            "buttons": [
                                {"type": "product", "title": "{{name}} {{price}}", "url": f"https://zoyee.in/{account.username}/product/64"},
                                {"type": "product", "title": "{{name}} {{price}}", "url": f"https://zoyee.in/{account.username}/product/61"}
                            ]
                        }
                    ]),
                    "messages": ["Welcome Product"]
                }
            },
            {
                "id": r131_not_following_id,
                "type": "action",
                "position": {"x": 1240, "y": 500},
                "data": {
                    "action_type": "send_dm",
                    "parent_event": "CHECK_FOLLOW",
                    "is_placeholder": False,
                    "dm_format": "loop_back",
                    "loop_target_id": r131_root_id,
                    "cf_branch": "not_following"
                }
            }
        ]

        edges131 = [
            {"id": "edge-131-1", "source": r131_t_id, "target": r131_c_id},
            {"id": "edge-131-act-0", "source": r131_c_id, "target": r131_root_id},
            {"id": f"edge-{r131_root_id}-profile", "source": r131_root_id, "target": r131_profile_id, "label": "Check follow"},
            {"id": f"edge-{r131_root_id}-fork", "source": r131_root_id, "target": r131_fork_id, "label": "I am following"},
            {"id": f"edge-{r131_fork_id}-following", "source": r131_fork_id, "target": r131_following_id, "label": "If Following"},
            {"id": f"edge-{r131_fork_id}-not-following", "source": r131_fork_id, "target": r131_not_following_id, "label": "If Not Following"},
            {"id": f"edge-loop-{r131_not_following_id}-{r131_root_id}", "source": r131_not_following_id, "target": r131_root_id, "label": "🔄 Loop Back"}
        ]

        rule131.visual_data = {"nodes": nodes131, "edges": edges131}
        rule131.save()
        print(f"[OK] Updated Rule 131 Layout with 40px gaps and zero overlap!")

    print("\nAll automations configured successfully for my_muscles_factory!")

if __name__ == "__main__":
    setup_my_muscles_factory_automations()
