import os
import logging
from decimal import Decimal
from django.utils import timezone
from apps.settings.models import SystemSettings
from apps.accounts.models import SellerKYC
from apps.crm.models import Settlement

logger = logging.getLogger(__name__)


def process_creator_payout(settlement_id):
    """
    Unified Payout Transfer Service for AnyDM (zoyee.in).
    Attempts transfers in order:
    1. Razorpay Route (Marketplace Split) if enable_razorpay_route=True & Creator has linked account.
    2. Razorpay Payouts API (RazorpayX) if enable_razorpay_payouts_api=True & Creator has bank/UPI details.
    3. Manual Settlement (Fallback): Records as PENDING for Admin Manual Transfer.
    """
    try:
        settlement = Settlement.objects.get(id=settlement_id)
    except Settlement.DoesNotExist:
        logger.error(f"Settlement ID {settlement_id} not found.")
        return {'success': False, 'mode': 'UNKNOWN', 'error': 'Settlement not found'}

    if settlement.status == 'PAID':
        return {'success': True, 'mode': settlement.transfer_mode, 'message': 'Already paid'}

    sys_settings = SystemSettings.get_settings()
    creator = settlement.seller
    kyc = SellerKYC.objects.filter(user=creator).first()

    RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_61r9Oaexv2tXjZ")
    RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "S7tK7rX35JqZJ35pL2O2x7w8")

    # ─────────────────────────────────────────────────────────────────────────────
    # OPTION A: RAZORPAY ROUTE (Marketplace Transfer)
    # ─────────────────────────────────────────────────────────────────────────────
    if sys_settings.enable_razorpay_route and kyc and kyc.razorpay_account_id:
        try:
            import razorpay
            client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

            amount_in_paise = int(settlement.seller_amount * 100)
            transfer_payload = {
                "account": kyc.razorpay_account_id,
                "amount": amount_in_paise,
                "currency": "INR",
                "notes": {
                    "settlement_id": str(settlement.id),
                    "seller_username": creator.username,
                    "type": settlement.settlement_type
                }
            }

            # Create transfer
            transfer_res = client.transfer.create(transfer_payload)
            transfer_id = transfer_res.get('id') if isinstance(transfer_res, dict) else str(transfer_res)

            settlement.transfer_mode = 'RAZORPAY_ROUTE'
            settlement.transfer_id = transfer_id
            settlement.status = 'PAID'
            settlement.paid_at = timezone.now()
            settlement.failure_reason = None
            settlement.save()

            logger.info(f"Settlement #{settlement.id} processed via Razorpay Route: {transfer_id}")
            return {'success': True, 'mode': 'RAZORPAY_ROUTE', 'transfer_id': transfer_id}
        except Exception as e:
            logger.warning(f"Razorpay Route attempt failed for Settlement #{settlement.id}: {str(e)}")
            settlement.failure_reason = f"Razorpay Route Failed: {str(e)}"
            settlement.save()

    # ─────────────────────────────────────────────────────────────────────────────
    # OPTION B: RAZORPAY PAYOUTS API (Direct Bank / UPI Payout)
    # ─────────────────────────────────────────────────────────────────────────────
    if sys_settings.enable_razorpay_payouts_api and kyc and (kyc.bank_account_number or kyc.upi_id):
        try:
            import requests
            account_number = os.getenv("RAZORPAYX_ACCOUNT_NUMBER", "")
            if account_number:
                payout_url = "https://api.razorpay.com/v1/payouts"
                amount_in_paise = int(settlement.seller_amount * 100)

                payload = {
                    "account_number": account_number,
                    "amount": amount_in_paise,
                    "currency": "INR",
                    "mode": "UPI" if kyc.upi_id else "NEFT",
                    "purpose": "payout",
                    "fund_account": {
                        "account_type": "vpa" if kyc.upi_id else "bank_account",
                        "vpa": {"address": kyc.upi_id} if kyc.upi_id else None,
                        "bank_account": {
                            "name": kyc.full_name or creator.get_full_name() or creator.username,
                            "ifsc": kyc.bank_ifsc,
                            "account_number": kyc.bank_account_number
                        } if not kyc.upi_id else None,
                        "contact": {
                            "name": kyc.full_name or creator.username,
                            "email": creator.email or "support@zoyee.in",
                            "contact": getattr(creator, 'phone_number', '9999999999'),
                            "type": "vendor"
                        }
                    },
                    "queue_if_low_balance": True,
                    "reference_id": f"SETTLEMENT_{settlement.id}",
                    "narration": f"AnyDM Settlement #{settlement.id}"
                }

                res = requests.post(
                    payout_url,
                    json=payload,
                    auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                    headers={"Content-Type": "application/json"}
                )

                if res.status_code in [200, 201]:
                    p_data = res.json()
                    payout_id = p_data.get("id")
                    utr = p_data.get("utr", "")

                    settlement.transfer_mode = 'RAZORPAY_PAYOUT'
                    settlement.transfer_id = payout_id
                    if utr:
                        settlement.utr_number = utr
                    settlement.status = 'PAID'
                    settlement.paid_at = timezone.now()
                    settlement.failure_reason = None
                    settlement.save()

                    logger.info(f"Settlement #{settlement.id} processed via Razorpay Payouts API: {payout_id}")
                    return {'success': True, 'mode': 'RAZORPAY_PAYOUT', 'payout_id': payout_id}
                else:
                    err_msg = res.text
                    logger.warning(f"Razorpay Payouts API returned status {res.status_code}: {err_msg}")
                    settlement.failure_reason = f"Razorpay Payouts API Failed: {err_msg}"
                    settlement.save()
        except Exception as e:
            logger.warning(f"Razorpay Payouts API attempt failed for Settlement #{settlement.id}: {str(e)}")
            settlement.failure_reason = f"Razorpay Payout Exception: {str(e)}"
            settlement.save()

    # ─────────────────────────────────────────────────────────────────────────────
    # OPTION C: MANUAL SETTLEMENT (PRIMARY CURRENT FALLBACK MODE)
    # ─────────────────────────────────────────────────────────────────────────────
    settlement.transfer_mode = 'MANUAL'
    settlement.status = 'PENDING'
    settlement.save()

    logger.info(f"Settlement #{settlement.id} queued for Manual Admin Processing.")
    return {
        'success': True,
        'mode': 'MANUAL',
        'status': 'PENDING',
        'message': 'Queued for Manual Settlement by Admin'
    }
