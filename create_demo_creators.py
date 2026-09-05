import os
import django
from decimal import Decimal
from datetime import timedelta

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone
from apps.accounts.models import CreatorCommission, SellerKYC

User = get_user_model()

def create_demo_data():
    print("Creating sample demo creators and referral payments...")

    # 1. Commission Mode Creator (15%)
    creator_comm, _ = User.objects.get_or_create(
        username='alex_creator_comm',
        defaults={
            'email': 'alex.creator@zoyee.in',
            'first_name': 'Alex (Commission Creator)',
            'plan': 'trial',
            'is_creator_vip': True,
            'creator_reward_type': 'commission',
            'creator_commission_percent': Decimal('15.00'),
            'referral_code': 'ALEX15'
        }
    )
    creator_comm.is_creator_vip = True
    creator_comm.creator_reward_type = 'commission'
    creator_comm.creator_commission_percent = Decimal('15.00')
    creator_comm.referral_code = 'ALEX15'
    creator_comm.save()

    SellerKYC.objects.update_or_create(
        user=creator_comm,
        defaults={
            'full_name': 'Alex Rivera',
            'bank_name': 'State Bank of India',
            'bank_account_number': '50100432198765',
            'bank_ifsc': 'SBIN0001234',
            'status': 'APPROVED'
        }
    )

    # 2. VIP Mode Creator (3 Months Pro)
    creator_vip, _ = User.objects.get_or_create(
        username='sarah_creator_vip',
        defaults={
            'email': 'sarah.vip@zoyee.in',
            'first_name': 'Sarah (VIP Pro Creator)',
            'plan': 'pro',
            'is_creator_vip': True,
            'creator_reward_type': 'vip',
            'premium_expires_at': timezone.now() + timedelta(days=90),
            'referral_code': 'SARAHVIP'
        }
    )
    creator_vip.plan = 'pro'
    creator_vip.is_creator_vip = True
    creator_vip.creator_reward_type = 'vip'
    creator_vip.premium_expires_at = timezone.now() + timedelta(days=90)
    creator_vip.referral_code = 'SARAHVIP'
    creator_vip.save()

    # 3. Referred users under Alex
    for i in range(1, 4):
        referred_cust, _ = User.objects.get_or_create(
            username=f'referred_buyer_{i}',
            defaults={
                'email': f'buyer{i}@example.com',
                'first_name': f'Buyer #{i}',
                'referred_by': creator_comm,
                'referred_by_set': True,
                'plan': 'pro',
                'pro_purchase_count': 1,
                'referral_paid_reward_given': True,
                'premium_expires_at': timezone.now() + timedelta(days=30)
            }
        )
        # Create a sample commission record
        status = 'paid' if i == 1 else 'pending'
        CreatorCommission.objects.get_or_create(
            creator=creator_comm,
            referred_user=referred_cust,
            defaults={
                'payment_amount': Decimal('499.00'),
                'commission_percent': Decimal('15.00'),
                'commission_amount': Decimal('74.85'),
                'status': status
            }
        )

    print("Successfully created demo creators and test referral purchases!")

if __name__ == '__main__':
    create_demo_data()
