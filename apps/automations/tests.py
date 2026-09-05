import json
from unittest.mock import patch, MagicMock
from django.test import TestCase
from rest_framework.test import APIClient
from django.utils import timezone
from apps.accounts.models import User, InstagramAccount
from apps.crm.models import Customer, CustomerInteraction
from apps.automations.models import AutomationRule, AutomationAction, AutomationExecution
from apps.automations.engine import execute_automation, execute_single_action


class AutomationEngineTests(TestCase):
    def setUp(self):
        # Create user and active seller account
        self.user = User.objects.create_user(
            username="testuser",
            email="testuser@example.com",
            password="testpassword123"
        )
        self.seller = InstagramAccount.objects.create(
            user=self.user,
            username="my_muscles_factory",
            instagram_user_id="17841400012345678",
            instagram_scoped_id="17841400012345678",
            access_token="EAABtesttoken123",
            is_active=True,
            is_enabled=True
        )
        self.user.active_instagram_account = self.seller
        self.user.save()

        # Create customer
        self.customer = Customer.objects.create(
            owner=self.seller,
            instagram_scoped_id="999888777666",
            username="fitness_fan",
            is_following_business=True
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch('apps.automations.engine.send_instagram_dm')
    @patch('apps.automations.engine.reply_instagram_comment')
    def test_comment_automation_execution(self, mock_reply_comment, mock_send_dm):
        """Tests that a comment trigger executes comment reply and private DM."""
        mock_reply_comment.return_value = (True, {"id": "comment_reply_123"})
        mock_send_dm.return_value = (True, {"message_id": "dm_msg_123"})

        rule = AutomationRule.objects.create(
            seller=self.seller,
            name="Comment Info Flow",
            rule_type="comment_automation",
            status="active",
            condition_match_type="contains",
            condition_keywords=["plan", "price"]
        )
        # Public reply
        AutomationAction.objects.create(
            rule=rule,
            order=0,
            action_type="reply_comment",
            messages=["Thanks! Sent workout plans to your DMs 💪"]
        )
        # Private DM
        AutomationAction.objects.create(
            rule=rule,
            order=1,
            action_type="send_dm",
            dm_format="text",
            messages=["Here are the workout plans!"]
        )

        interaction = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=self.customer,
            direction="INBOUND",
            event_type="COMMENT",
            instagram_event_id="comm_9999",
            message_text="Can you share the workout plan and price please?"
        )

        execute_automation(interaction)

        # Assert comment reply sent
        mock_reply_comment.assert_called_once_with(
            self.seller,
            "comm_9999",
            "Thanks! Sent workout plans to your DMs 💪"
        )

        # Assert private DM sent via comment_id recipient_type
        mock_send_dm.assert_called_once_with(
            self.seller,
            "comm_9999",
            {"text": "Here are the workout plans!"},
            dm_format="text",
            recipient_type="comment_id"
        )

        # Check execution record
        exec_record = AutomationExecution.objects.filter(rule=rule).first()
        self.assertIsNotNone(exec_record)
        self.assertEqual(exec_record.status, "success")

    @patch('apps.automations.engine.send_instagram_dm')
    def test_dm_button_template_and_postback_execution(self, mock_send_dm):
        """Tests initial DM button template trigger and subsequent postback action."""
        mock_send_dm.return_value = (True, {"message_id": "dm_msg_200"})

        rule = AutomationRule.objects.create(
            seller=self.seller,
            name="Welcome DM Flow",
            rule_type="dm_automation",
            status="active",
            condition_match_type="contains",
            condition_keywords=["hello", "hi"]
        )
        # Level 1 DM Button Template
        btn_action = AutomationAction.objects.create(
            rule=rule,
            order=0,
            action_type="send_dm",
            dm_format="button_template",
            messages=["Welcome to My Muscles Factory! Choose an option:"],
            button_template_payload={
                "buttons": [
                    {"type": "postback", "title": "💪 Workout Plans", "payload": "BTN_PLANS"},
                    {"type": "postback", "title": "🥗 Diet Guide", "payload": "BTN_DIET"},
                ]
            }
        )

        # Level 2 Sub-reply for BTN_PLANS
        sub_action = AutomationAction.objects.create(
            rule=rule,
            order=1,
            action_type="send_dm",
            dm_format="text",
            parent_event="BTN_PLANS",
            messages=["Here are our 3 tailored Workout Plans: Beginner, Hypertrophy, Powerlifting."]
        )

        # 1. Trigger initial DM
        interaction1 = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=self.customer,
            direction="INBOUND",
            event_type="DM",
            message_text="Hi there"
        )
        execute_automation(interaction1)

        mock_send_dm.assert_called_with(
            self.seller,
            self.customer.instagram_scoped_id,
            {
                "text": "Welcome to My Muscles Factory! Choose an option:",
                "buttons": [
                    {"type": "postback", "title": "💪 Workout Plans", "payload": "BTN_PLANS"},
                    {"type": "postback", "title": "🥗 Diet Guide", "payload": "BTN_DIET"},
                ]
            },
            dm_format="button_template",
            recipient_type="id"
        )

        # 2. Trigger Postback click for BTN_PLANS
        interaction2 = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=self.customer,
            direction="INBOUND",
            event_type="CLICK",
            metadata={"postback": {"payload": "BTN_PLANS"}},
            message_text="Postback: BTN_PLANS"
        )
        execute_automation(interaction2)

        mock_send_dm.assert_called_with(
            self.seller,
            self.customer.instagram_scoped_id,
            {"text": "Here are our 3 tailored Workout Plans: Beginner, Hypertrophy, Powerlifting."},
            dm_format="text",
            recipient_type="id"
        )

    @patch('apps.automations.engine.send_instagram_dm')
    def test_back_loop_execution_to_root_button_template(self, mock_send_dm):
        """Tests Back Loop feature: clicking a back-loop postback re-delivers the target button template."""
        mock_send_dm.return_value = (True, {"message_id": "dm_msg_300"})

        visual_nodes = [
            {
                "id": "node-t-1",
                "type": "trigger",
                "ruleType": "dm_automation",
                "data": {"target_mode": "every"}
            },
            {
                "id": "node-a-welcome",
                "type": "action",
                "data": {
                    "action_type": "send_dm",
                    "dm_format": "button_template",
                    "button_template_text": "Main Menu - My Muscles Factory",
                    "messages": ["Main Menu - My Muscles Factory"],
                    "button_template_buttons_json": json.dumps([
                        {"type": "postback", "title": "💪 Plans", "payload": "GO_PLANS"}
                    ])
                }
            },
            {
                "id": "node-a-plans",
                "type": "action",
                "data": {
                    "action_type": "send_dm",
                    "dm_format": "button_template",
                    "parent_event": "GO_PLANS",
                    "button_template_text": "Choose a plan or go back:",
                    "messages": ["Choose a plan or go back:"],
                    "button_template_buttons_json": json.dumps([
                        {"type": "postback", "title": "🔄 Back to Menu", "payload": "LOOP_BACK_TO_MENU"}
                    ])
                }
            },
            {
                "id": "node-a-loop-card",
                "type": "action",
                "data": {
                    "action_type": "send_dm",
                    "dm_format": "loop_back",
                    "parent_event": "LOOP_BACK_TO_MENU",
                    "loop_target_id": "node-a-welcome"
                }
            }
        ]

        rule = AutomationRule.objects.create(
            seller=self.seller,
            name="Back Loop Menu Flow",
            rule_type="dm_automation",
            status="active",
            condition_match_type="any",
            visual_data={"nodes": visual_nodes, "edges": []}
        )

        root_action = AutomationAction.objects.create(
            rule=rule,
            order=0,
            action_type="send_dm",
            dm_format="button_template",
            messages=["Main Menu - My Muscles Factory"],
            button_template_payload={
                "buttons": [{"type": "postback", "title": "💪 Plans", "payload": "GO_PLANS"}]
            }
        )

        plans_action = AutomationAction.objects.create(
            rule=rule,
            order=1,
            action_type="send_dm",
            dm_format="button_template",
            parent_event="GO_PLANS",
            messages=["Choose a plan or go back:"],
            button_template_payload={
                "buttons": [{"type": "postback", "title": "🔄 Back to Menu", "payload": "LOOP_BACK_TO_MENU"}]
            }
        )

        loop_action = AutomationAction.objects.create(
            rule=rule,
            order=2,
            action_type="send_dm",
            dm_format="loop_back",
            parent_event="LOOP_BACK_TO_MENU",
            loop_target_id="node-a-welcome",
            messages=["Loop Back"]
        )

        # Trigger the Loop Back Postback
        interaction = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=self.customer,
            direction="INBOUND",
            event_type="CLICK",
            metadata={"postback": {"payload": "LOOP_BACK_TO_MENU"}},
            message_text="Postback: LOOP_BACK_TO_MENU"
        )

        execute_automation(interaction)

        # Verify that send_instagram_dm was called with the target welcome card's button template!
        mock_send_dm.assert_called_once_with(
            self.seller,
            self.customer.instagram_scoped_id,
            {
                "text": "Main Menu - My Muscles Factory",
                "buttons": [{"type": "postback", "title": "💪 Plans", "payload": "GO_PLANS"}]
            },
            dm_format="button_template",
            recipient_type="id"
        )

        # Check execution log
        exec_record = AutomationExecution.objects.filter(rule=rule, trigger_event_type="CLICK").first()
        self.assertIsNotNone(exec_record)
        self.assertEqual(exec_record.status, "success")
        self.assertEqual(exec_record.actions_log[0]["dm_format"], "button_template")

    @patch('apps.automations.engine.send_instagram_dm')
    def test_back_loop_execution_to_carousel(self, mock_send_dm):
        """Tests Back Loop feature targeting a Generic Template (Carousel) node."""
        mock_send_dm.return_value = (True, {"message_id": "dm_msg_400"})

        visual_nodes = [
            {
                "id": "node-a-carousel",
                "type": "action",
                "data": {
                    "action_type": "send_dm",
                    "dm_format": "generic_template",
                    "generic_template_elements_json": json.dumps([
                        {
                            "title": "Protein Shake Pro",
                            "subtitle": "Best for muscle recovery",
                            "buttons": [{"type": "postback", "title": "Details", "payload": "SHAKE_DETAILS"}]
                        }
                    ])
                }
            },
            {
                "id": "node-a-loop-carousel",
                "type": "action",
                "data": {
                    "action_type": "send_dm",
                    "dm_format": "loop_back",
                    "parent_event": "RESTART_CATALOG",
                    "loop_target_id": "node-a-carousel"
                }
            }
        ]

        rule = AutomationRule.objects.create(
            seller=self.seller,
            name="Carousel Loop Flow",
            rule_type="dm_automation",
            status="active",
            condition_match_type="any",
            visual_data={"nodes": visual_nodes, "edges": []}
        )

        loop_action = AutomationAction.objects.create(
            rule=rule,
            order=0,
            action_type="send_dm",
            dm_format="loop_back",
            parent_event="RESTART_CATALOG",
            loop_target_id="node-a-carousel",
            messages=["Loop Back"]
        )

        interaction = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=self.customer,
            direction="INBOUND",
            event_type="CLICK",
            metadata={"postback": {"payload": "RESTART_CATALOG"}},
            message_text="Postback: RESTART_CATALOG"
        )

        execute_automation(interaction)

        mock_send_dm.assert_called_once_with(
            self.seller,
            self.customer.instagram_scoped_id,
            {
                "elements": [
                    {
                        "title": "Protein Shake Pro",
                        "subtitle": "Best for muscle recovery",
                        "buttons": [{"type": "postback", "title": "Details", "payload": "SHAKE_DETAILS"}]
                    }
                ]
            },
            dm_format="generic_template",
            recipient_type="id"
        )

    @patch('apps.automations.engine.send_instagram_dm')
    def test_back_loop_execution_to_check_follow(self, mock_send_dm):
        """Tests Back Loop feature targeting a Check Follow Gate node."""
        mock_send_dm.return_value = (True, {"message_id": "dm_msg_450"})

        visual_nodes = [
            {
                "id": "node-a-gate",
                "type": "action",
                "data": {
                    "action_type": "send_dm",
                    "dm_format": "check_follow",
                    "following_format": "text",
                    "following_text": "Unlocked! You are an active follower.",
                    "not_following_format": "button_template",
                    "not_following_text": "Please follow us to unlock:",
                    "not_following_button_text": "👉 Follow Us",
                    "not_following_profile_url": "https://instagram.com/my_muscles_factory"
                }
            },
            {
                "id": "node-a-recheck-loop",
                "type": "action",
                "data": {
                    "action_type": "send_dm",
                    "dm_format": "loop_back",
                    "parent_event": "RECHECK_FOLLOW",
                    "loop_target_id": "node-a-gate"
                }
            }
        ]

        rule = AutomationRule.objects.create(
            seller=self.seller,
            name="Gate Loop Flow",
            rule_type="dm_automation",
            status="active",
            condition_match_type="any",
            visual_data={"nodes": visual_nodes, "edges": []}
        )

        loop_action = AutomationAction.objects.create(
            rule=rule,
            order=0,
            action_type="send_dm",
            dm_format="loop_back",
            parent_event="RECHECK_FOLLOW",
            loop_target_id="node-a-gate",
            messages=["Loop Back"]
        )

        # Customer is following -> resolves to following branch
        interaction = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=self.customer,
            direction="INBOUND",
            event_type="CLICK",
            metadata={"postback": {"payload": "RECHECK_FOLLOW"}},
            message_text="Postback: RECHECK_FOLLOW"
        )

        execute_automation(interaction)

        mock_send_dm.assert_called_once_with(
            self.seller,
            self.customer.instagram_scoped_id,
            {"text": "Unlocked! You are an active follower."},
            dm_format="text",
            recipient_type="id"
        )

    @patch('apps.automations.engine.send_instagram_dm')
    def test_story_reply_and_media_share_automation(self, mock_send_dm):
        """Tests Story reply and Media share automation triggers."""
        mock_send_dm.return_value = (True, {"message_id": "dm_msg_600"})

        # Story automation
        story_rule = AutomationRule.objects.create(
            seller=self.seller,
            name="Story Reply Flow",
            rule_type="story_automation",
            status="active",
            condition_match_type="contains",
            condition_keywords=["fire", "gym"]
        )
        AutomationAction.objects.create(
            rule=story_rule,
            order=0,
            action_type="send_dm",
            dm_format="text",
            messages=["Thanks for replying to our story! 🔥"]
        )

        interaction_story = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=self.customer,
            direction="INBOUND",
            event_type="STORY_REPLY",
            message_text="This gym session is fire!"
        )
        execute_automation(interaction_story)

        mock_send_dm.assert_called_with(
            self.seller,
            self.customer.instagram_scoped_id,
            {"text": "Thanks for replying to our story! 🔥"},
            dm_format="text",
            recipient_type="id"
        )

        # Media share automation
        mock_send_dm.reset_mock()
        share_rule = AutomationRule.objects.create(
            seller=self.seller,
            name="Post Share Flow",
            rule_type="media_share_dm",
            status="active",
            condition_match_type="any"
        )
        AutomationAction.objects.create(
            rule=share_rule,
            order=0,
            action_type="send_dm",
            dm_format="text",
            messages=["Thanks for sharing this post! Here is 10% off: SHARE10"]
        )

        interaction_share = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=self.customer,
            direction="INBOUND",
            event_type="DM",
            message_type="POST",
            media_id="ig_media_8888",
            message_text="Shared a post"
        )
        execute_automation(interaction_share)

        mock_send_dm.assert_called_with(
            self.seller,
            self.customer.instagram_scoped_id,
            {"text": "Thanks for sharing this post! Here is 10% off: SHARE10"},
            dm_format="text",
            recipient_type="id"
        )

    @patch('apps.automations.engine.send_instagram_dm')
    def test_follower_gate_blocked_vs_allowed(self, mock_send_dm):
        """Tests follower gate blocking non-followers and allowing followers."""
        mock_send_dm.return_value = (True, {"message_id": "dm_msg_500"})

        rule = AutomationRule.objects.create(
            seller=self.seller,
            name="Follower Only VIP Deal",
            rule_type="dm_automation",
            status="active",
            condition_match_type="contains",
            condition_keywords=["vip"],
            follower_gate_enabled=True,
            follower_gate_messages=["Please follow @my_muscles_factory to unlock VIP discount!"]
        )
        AutomationAction.objects.create(
            rule=rule,
            order=0,
            action_type="send_dm",
            dm_format="text",
            messages=["Here is your VIP 50% discount code: FACTORY50"]
        )

        non_follower = Customer.objects.create(
            owner=self.seller,
            instagram_scoped_id="111222333444",
            username="random_user",
            is_following_business=False
        )
        interaction_nf = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=non_follower,
            direction="INBOUND",
            event_type="DM",
            message_text="Give me vip code"
        )
        execute_automation(interaction_nf)

        # Verify follower gate warning was sent instead of VIP code
        mock_send_dm.assert_called_with(
            self.seller,
            "111222333444",
            {"text": "Please follow @my_muscles_factory to unlock VIP discount!"},
            "text"
        )

        # 2. Follower tries to trigger
        mock_send_dm.reset_mock()
        interaction_f = CustomerInteraction.objects.create(
            seller_account=self.seller,
            customer=self.customer,
            direction="INBOUND",
            event_type="DM",
            message_text="Give me vip code"
        )
        execute_automation(interaction_f)

        # Verify normal VIP action delivered
        mock_send_dm.assert_called_with(
            self.seller,
            self.customer.instagram_scoped_id,
            {"text": "Here is your VIP 50% discount code: FACTORY50"},
            dm_format="text",
            recipient_type="id"
        )

    def test_automation_api_create_and_retrieve_with_back_loop(self):
        """Tests creating an automation with a Back Loop action via API and retrieving it."""
        payload = {
            "name": "API Full Flow with Loop Back",
            "status": "active",
            "nodes": [
                {
                    "id": "node-t-api",
                    "type": "trigger",
                    "ruleType": "comment_automation",
                    "data": {"target_mode": "every"}
                },
                {
                    "id": "node-c-api",
                    "type": "condition",
                    "data": {"match_type": "contains", "keywords": ["workout", "fitness"]}
                },
                {
                    "id": "node-a-root",
                    "type": "action",
                    "position": {"x": 1000, "y": 100},
                    "data": {
                        "action_type": "send_dm",
                        "dm_format": "button_template",
                        "button_template_text": "Select your fitness goal:",
                        "button_template_buttons_json": json.dumps([
                            {"type": "postback", "title": "Muscle Gain", "payload": "GOAL_GAIN"}
                        ])
                    }
                },
                {
                    "id": "node-a-loop",
                    "type": "action",
                    "position": {"x": 1400, "y": 200},
                    "data": {
                        "action_type": "send_dm",
                        "dm_format": "loop_back",
                        "parent_event": "LOOP_TO_GOALS",
                        "loop_target_id": "node-a-root"
                    }
                }
            ],
            "edges": [
                {"id": "e1", "source": "node-t-api", "target": "node-c-api"},
                {"id": "e2", "source": "node-c-api", "target": "node-a-root"},
                {"id": "e3", "source": "node-a-root", "target": "node-a-loop", "label": "Loop to Goals"}
            ]
        }

        response = self.client.post("/api/automations/", payload, format="json")
        self.assertEqual(response.status_code, 200)
        rule_id = response.data.get("id")
        self.assertIsNotNone(rule_id)

        # Fetch rule details
        detail_resp = self.client.get(f"/api/automations/{rule_id}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.data["name"], "API Full Flow with Loop Back")
        
        # Check action data returned
        actions = detail_resp.data["actions"]
        self.assertEqual(len(actions), 2)
        loop_act = next((a for a in actions if a["dm_format"] == "loop_back"), None)
        self.assertIsNotNone(loop_act)
        self.assertEqual(loop_act["loop_target_id"], "node-a-root")
        self.assertEqual(loop_act["parent_event"], "LOOP_TO_GOALS")
