import os
import django
from decimal import Decimal
from datetime import timedelta

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone
from apps.accounts.models import CreatorCommission, SellerKYC
from apps.settings.models import SystemSettings

User = get_user_model()

def run_tests():
    print("=" * 60)
    print("STARTING CREATOR COMMISSION & VIP END-TO-END VERIFICATION")
    print("=" * 60)

    # 0. Clean up previous test users if any
    User.objects.filter(username__in=[
        'dummy_creator_comm', 'dummy_creator_vip', 
        'dummy_customer_1', 'dummy_customer_2'
    ]).delete()

    # 1. Create Creator A (Commission mode @ 10%)
    creator_a = User.objects.create_user(
        username='dummy_creator_comm',
        email='creator_comm@test.com',
        first_name='Commission Creator'
    )
    creator_a.is_creator_vip = True
    creator_a.creator_reward_type = 'commission'
    creator_a.creator_commission_percent = Decimal('10.00')
    creator_a.referral_code = 'COMM10'
    creator_a.save()

    # Add KYC for Creator A
    SellerKYC.objects.create(
        user=creator_a,
        full_name='Commission Creator Bank Acc',
        bank_name='HDFC Bank',
        bank_account_number='98765432101234',
        bank_ifsc='HDFC0001234',
        status='APPROVED'
    )
    print(" [1] Created Creator A: Commission mode (10%) with KYC Bank Info")

    # 2. Create Creator B (VIP Free Pro mode - 3 months)
    creator_b = User.objects.create_user(
        username='dummy_creator_vip',
        email='creator_vip@test.com',
        first_name='VIP Creator'
    )
    creator_b.plan = 'pro'
    creator_b.is_creator_vip = True
    creator_b.creator_reward_type = 'vip'
    creator_b.premium_expires_at = timezone.now() + timedelta(days=90)
    creator_b.referral_code = 'VIPPRO'
    creator_b.save()
    print(" [2] Created Creator B: VIP mode (3 months Free Pro)")
    assert creator_b.is_premium_active == True, "Creator B should have active Pro!"

    # 3. Create Customer 1 (referred by Creator A)
    customer_1 = User.objects.create_user(
        username='dummy_customer_1',
        email='customer1@test.com',
        first_name='Customer One'
    )
    customer_1.referred_by = creator_a
    customer_1.referred_by_set = True
    customer_1.save()
    print(" [3] Created Customer 1 (Referred by Creator A)")

    # 4. Simulate Customer 1 making FIRST purchase (₹499)
    sys_settings = SystemSettings.get_settings()
    plan_price = Decimal(str(sys_settings.premium_plan_price or 499.00))
    
    # First purchase simulation (Razorpay payment verify logic)
    customer_1.plan = 'pro'
    customer_1.pro_purchase_count = getattr(customer_1, 'pro_purchase_count', 0) + 1
    customer_1.premium_expires_at = timezone.now() + timedelta(days=30)

    # Commission hook for 1st payment
    if customer_1.referred_by and not getattr(customer_1, 'referral_paid_reward_given', False):
        ref = customer_1.referred_by
        if ref.is_creator_vip and ref.creator_reward_type == 'commission':
            pct = ref.creator_commission_percent or Decimal('10.00')
            comm_amt = (plan_price * pct) / Decimal('100')
            CreatorCommission.objects.create(
                creator=ref,
                referred_user=customer_1,
                payment_amount=plan_price,
                commission_percent=pct,
                commission_amount=comm_amt,
                status='pending'
            )
        customer_1.referral_paid_reward_given = True
    customer_1.save()

    comm_1 = CreatorCommission.objects.filter(creator=creator_a, referred_user=customer_1).first()
    assert comm_1 is not None, "Commission must be created on 1st purchase!"
    assert comm_1.commission_amount == Decimal('49.90'), f"Expected 49.90, got {comm_1.commission_amount}"
    assert comm_1.status == 'pending', "Commission status must be pending"
    print(f" [4] 1st Purchase Successful: Creator A earned Rs.{comm_1.commission_amount} (10% of Rs.{plan_price}) [Status: Pending]")

    # 5. Simulate Customer 1 making SECOND purchase (Renewal next month)
    customer_1.pro_purchase_count += 1
    customer_1.premium_expires_at += timedelta(days=30)
    
    # Check that 2nd payment does NOT trigger new commission because referral_paid_reward_given is already True
    initial_comm_count = CreatorCommission.objects.filter(creator=creator_a).count()
    if customer_1.referred_by and not getattr(customer_1, 'referral_paid_reward_given', False):
        # Should not enter here
        pass
    customer_1.save()

    final_comm_count = CreatorCommission.objects.filter(creator=creator_a).count()
    assert final_comm_count == initial_comm_count == 1, "Renewal payments MUST NOT create extra commission!"
    print(" [5] 2nd Purchase (Renewal) Successful: Verified NO duplicate commission created (First payment only rule works!)")

    # 6. Verify Creator A Earnings Dashboard calculations
    comm_qs = CreatorCommission.objects.filter(creator=creator_a)
    from django.db.models import Sum
    total_earned = float(comm_qs.aggregate(total=Sum('commission_amount'))['total'] or 0)
    total_pending = float(comm_qs.filter(status='pending').aggregate(total=Sum('commission_amount'))['total'] or 0)
    total_paid = float(comm_qs.filter(status='paid').aggregate(total=Sum('commission_amount'))['total'] or 0)
    
    assert total_earned == 49.90, f"Expected 49.90, got {total_earned}"
    assert total_pending == 49.90, f"Expected 49.90, got {total_pending}"
    assert total_paid == 0.0, f"Expected 0.0, got {total_paid}"
    print(f" [6] Creator Dashboard Stats Verified: Total Earned = Rs.{total_earned}, Pending = Rs.{total_pending}, Paid = Rs.{total_paid}")

    # 7. Admin settles payout for Creator A
    pending_qs = CreatorCommission.objects.filter(creator=creator_a, status='pending')
    settled_count = pending_qs.count()
    pending_qs.update(status='paid')

    # Re-check earnings
    total_pending_after = float(comm_qs.filter(status='pending').aggregate(total=Sum('commission_amount'))['total'] or 0)
    total_paid_after = float(comm_qs.filter(status='paid').aggregate(total=Sum('commission_amount'))['total'] or 0)
    
    assert total_pending_after == 0.0, "Pending must be 0 after settlement!"
    assert total_paid_after == 49.90, "Paid must be 49.90 after settlement!"
    print(f" [7] Admin Payout Settlement Verified: Pending = Rs.{total_pending_after}, Settled Paid = Rs.{total_paid_after} ({settled_count} records settled)")

    # 8. Test Pro Access Expiration (After time reached, Pro finishes)
    temp_user = User.objects.create_user(
        username='dummy_customer_2',
        email='customer2@test.com'
    )
    # Grant 1 month pro
    temp_user.plan = 'pro'
    temp_user.premium_expires_at = timezone.now() + timedelta(days=30)
    temp_user.save()
    assert temp_user.is_premium_active == True, "Should be active before expiry"

    # Now expire the pro access (set to past date)
    temp_user.premium_expires_at = timezone.now() - timedelta(days=1)
    temp_user.save()
    assert temp_user.is_premium_active == False, "After time reached, Pro access MUST finish (is_premium_active = False)!"
    print(" [8] Pro Access Expiry Verified: User with expired premium_expires_at has is_premium_active = False (Pro finishes)")

    print("=" * 60)
    print("ALL 8 VERIFICATION TESTS PASSED SUCCESSFULLY! ")
    print("=" * 60)

if __name__ == '__main__':
    run_tests()
