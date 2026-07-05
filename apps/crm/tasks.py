import logging
from celery import shared_task

from .models import Customer
from .utils import sync_customer_profile


logger = logging.getLogger(__name__)

@shared_task
def process_enquiry_background_task(interaction_id):
    """
    Asynchronous Celery task to handle CRM enquiry matching and creation.
    """
    
    from .models import CustomerInteraction
    from .views import process_interaction_all

    try:
        interaction = CustomerInteraction.objects.get(id=interaction_id)
        enquiry = process_interaction_all(interaction)
        if enquiry:
            logger.info(f"Asynchronous processing succeeded for Enquiry ID: {enquiry.id}")
    except CustomerInteraction.DoesNotExist:
        logger.error(f"CustomerInteraction with ID {interaction_id} not found.")
    except Exception as e:
        logger.error(f"Error executing background CRM matching task for ID {interaction_id}: {e}", exc_info=True)


@shared_task
def reconcile_unprocessed_interactions():
    """
    Periodic backup task to process interactions missed during Celery/Redis downtime.
    Ensures zero CRM lead data loss.
    """
    from .models import CustomerInteraction

    # Fetch inbound interactions where CRM processing has not been finalized
    unprocessed_interactions = CustomerInteraction.objects.filter(
        direction="INBOUND"
    ).exclude(metadata__crm_processed=True)[:100]  # Chunk limit to prevent memory spikes

    reconciled_count = 0
    for interaction in unprocessed_interactions:
        try:
            process_enquiry_background_task.delay(interaction.id)
            reconciled_count += 1
        except Exception as e:
            logger.error(f"Failed to queue reconciliation task for interaction {interaction.id}: {e}")

    if reconciled_count > 0:
        logger.info(f"Reconciliation task successfully enqueued {reconciled_count} unprocessed interactions.")







@shared_task
def sync_customer_profile_task(customer_id):
    customer = Customer.objects.get(id=customer_id)
    sync_customer_profile(customer)


@shared_task
def process_ai_response_task(interaction_id):
    """
    Asynchronous Celery task to trigger AI response generation using Gemini.
    """
    from .ai_assistant import process_ai_response
    try:
        process_ai_response(interaction_id)
    except Exception as e:
        logger.error(f"Error in process_ai_response_task for interaction {interaction_id}: {e}", exc_info=True)


import time
@shared_task
def fake_redis_task():
    print("Django working: task received from cron/job trigger")
    time.sleep(2)
    print("Task executed successfully")
    return "done"