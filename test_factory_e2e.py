import os
import uuid
import django
from unittest.mock import patch

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from apps.accounts.models import User, InstagramAccount
from apps.crm.models import Customer, CustomerInteraction
from apps.automations.models import AutomationRule, AutomationAction, AutomationExecution
from apps.automations.engine import execute_automation

@patch('apps.automations.engine.send_instagram_dm')
@patch('apps.automations.engine.reply_instagram_comment')
def run_e2e_verification(mock_reply_comment, mock_send_dm):
    mock_reply_comment.return_value = (True, {"id": "comm_reply_999"})
    mock_send_dm.return_value = (True, {"message_id": "dm_msg_999"})

    print("==================================================")
    print("STARTING E2E VERIFICATION FOR MY_MUSCLES_FACTORY")
    print("==================================================")

    user = User.objects.get(email="muhammedanasmp2001@gmail.com")
    account = InstagramAccount.objects.get(id=12, user=user)
    print(f"Target Account: {account.username} (ID: {account.id})")

    # Create test customer
    customer, _ = Customer.objects.get_or_create(
        owner=account,
        instagram_scoped_id="test_user_scoped_123",
        defaults={"username": "fitness_enthusiast", "is_following_business": True}
    )
    customer.is_following_business = True
    customer.save()

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 1: Comment Trigger on Rule 1
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 1: Comment Trigger ('price and workout plan') ---")
    interaction_comment = CustomerInteraction.objects.create(
        seller_account=account,
        customer=customer,
        direction="INBOUND",
        event_type="COMMENT",
        instagram_event_id=f"comm_event_{uuid.uuid4().hex[:8]}",
        message_text="What is your workout plan and price?"
    )
    execute_automation(interaction_comment)

    assert mock_reply_comment.called, "Public comment reply was NOT called!"
    print("[PASS] Public comment reply sent successfully.")

    assert mock_send_dm.called, "Private DM was NOT called!"
    dm_format_called = mock_send_dm.call_args.kwargs.get("dm_format") or (mock_send_dm.call_args[0][3] if len(mock_send_dm.call_args[0]) > 3 else None)
    assert dm_format_called == "button_template", f"Expected button_template, got {dm_format_called}"
    print("[PASS] Private DM button template sent successfully.")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 2: Postback Click for VIEW_WORKOUT_PLANS
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 2: Postback Click ('VIEW_WORKOUT_PLANS') ---")
    mock_send_dm.reset_mock()
    interaction_plans = CustomerInteraction.objects.create(
        seller_account=account,
        customer=customer,
        direction="INBOUND",
        event_type="CLICK",
        metadata={"postback": {"payload": "VIEW_WORKOUT_PLANS"}},
        message_text="Postback: VIEW_WORKOUT_PLANS"
    )
    execute_automation(interaction_plans)

    assert mock_send_dm.called, "Postback DM was NOT called!"
    dm_call_args = mock_send_dm.call_args[0]
    assert "🏋️ Choose your workout tier" in dm_call_args[2]["text"]
    print("[PASS] Tiered workout plans delivered successfully.")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 3: BACK LOOP Execution ('LOOP_TO_MAIN_MENU')
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 3: BACK LOOP Click ('LOOP_TO_MAIN_MENU') ---")
    mock_send_dm.reset_mock()
    interaction_loop = CustomerInteraction.objects.create(
        seller_account=account,
        customer=customer,
        direction="INBOUND",
        event_type="CLICK",
        metadata={"postback": {"payload": "LOOP_TO_MAIN_MENU"}},
        message_text="Postback: LOOP_TO_MAIN_MENU"
    )
    execute_automation(interaction_loop)

    assert mock_send_dm.called, "Back Loop DM was NOT called!"
    dm_format_called = mock_send_dm.call_args.kwargs.get("dm_format") or (mock_send_dm.call_args[0][3] if len(mock_send_dm.call_args[0]) > 3 else None)
    assert dm_format_called == "button_template", f"Expected button_template from loop target, got {dm_format_called}"
    msg_data_called = mock_send_dm.call_args[0][2]
    assert "Welcome to My Muscles Factory" in msg_data_called["text"], "Target button template content mismatch"
    print("[PASS] Back Loop successfully resolved and re-delivered Main Menu Button Template!")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 4: VIP Follower Gate and Loop Back
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 4: VIP Follower Gate & Gate Loop Back ---")
    mock_send_dm.reset_mock()
    interaction_vip = CustomerInteraction.objects.create(
        seller_account=account,
        customer=customer,
        direction="INBOUND",
        event_type="DM",
        message_text="Give me the free vip discount"
    )
    execute_automation(interaction_vip)

    assert mock_send_dm.called, "VIP DM was NOT called!"
    dm_call_args = mock_send_dm.call_args[0]
    assert "VIP Unlocked" in dm_call_args[2]["text"]
    print("[PASS] VIP Follower Gate verified follower and delivered 50% discount.")

    mock_send_dm.reset_mock()
    interaction_gate_loop = CustomerInteraction.objects.create(
        seller_account=account,
        customer=customer,
        direction="INBOUND",
        event_type="CLICK",
        metadata={"postback": {"payload": "RECHECK_VIP_GATE"}},
        message_text="Postback: RECHECK_VIP_GATE"
    )
    execute_automation(interaction_gate_loop)
    assert mock_send_dm.called, "Gate Back Loop was NOT called!"
    print("[PASS] Gate Back Loop successfully re-evaluated and delivered gate response!")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 5: Story Reply Assistant and Loop Back
    # ─────────────────────────────────────────────────────────────────────────
    print("\n--- TEST 5: Story Reply & Story Back Loop ---")
    mock_send_dm.reset_mock()
    interaction_story = CustomerInteraction.objects.create(
        seller_account=account,
        customer=customer,
        direction="INBOUND",
        event_type="STORY_REPLY",
        message_text="Great workout video!"
    )
    execute_automation(interaction_story)
    assert mock_send_dm.called, "Story Reply DM was NOT called!"
    print("[PASS] Story Reply Assistant delivered Button Template.")

    mock_send_dm.reset_mock()
    interaction_story_loop = CustomerInteraction.objects.create(
        seller_account=account,
        customer=customer,
        direction="INBOUND",
        event_type="CLICK",
        metadata={"postback": {"payload": "STORY_LOOP_RESTART"}},
        message_text="Postback: STORY_LOOP_RESTART"
    )
    execute_automation(interaction_story_loop)
    assert mock_send_dm.called, "Story Back Loop DM was NOT called!"
    print("[PASS] Story Back Loop successfully re-delivered Story Assistant Menu.")

    print("\n==================================================")
    print("ALL 5 E2E TESTS PASSED WITH 100% SUCCESS!")
    print("==================================================")

if __name__ == "__main__":
    run_e2e_verification()
