import logging
from celery import shared_task
from django.utils import timezone
from .models import ScheduledPost
from .publishing import execute_publish_post

logger = logging.getLogger(__name__)


@shared_task
def publish_scheduled_post_task(scheduled_post_id):
    """
    Asynchronous Celery task to publish a single ScheduledPost to Instagram.
    """
    try:
        post = ScheduledPost.objects.get(id=scheduled_post_id)
        if post.status not in ['SCHEDULED', 'PROCESSING', 'FAILED', 'DRAFT']:
            logger.info(f"[TASKS] Skipping post #{scheduled_post_id} with status '{post.status}'")
            return False

        logger.info(f"[TASKS] Executing publish task for ScheduledPost #{post.id} ({post.post_type})")
        success, result = execute_publish_post(post)
        return success
    except ScheduledPost.DoesNotExist:
        logger.error(f"[TASKS] ScheduledPost with ID {scheduled_post_id} does not exist.")
        return False
    except Exception as e:
        logger.error(f"[TASKS] Unexpected error publishing post #{scheduled_post_id}: {e}", exc_info=True)
        return False


@shared_task
def process_due_scheduled_posts_task():
    """
    Periodic scheduler task: queries all ScheduledPost items that are due (scheduled_at <= now)
    and executes them. Runs via Celery Beat or manual trigger.
    """
    now = timezone.now()
    due_posts = ScheduledPost.objects.filter(
        status='SCHEDULED',
        scheduled_at__lte=now
    )

    count = due_posts.count()
    if count > 0:
        logger.info(f"[SCHEDULER] Found {count} due Instagram post(s) to process.")
        for post in due_posts:
            # Mark as processing immediately to avoid duplicate pickup
            post.status = 'PROCESSING'
            post.save(update_fields=['status'])
            # Trigger celery task asynchronously (or execute directly if celery offline)
            try:
                publish_scheduled_post_task.delay(post.id)
            except Exception:
                publish_scheduled_post_task(post.id)
    return count
