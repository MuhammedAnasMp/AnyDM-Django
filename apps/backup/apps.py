import logging
from django.apps import AppConfig
from django.db.models.signals import post_migrate

logger = logging.getLogger(__name__)


def ensure_periodic_tasks(sender, **kwargs):
    """
    Automatically creates default Celery Beat periodic tasks in the DB
    if they do not already exist. Safe and idempotent.
    """
    using_db = kwargs.get("using", "default")
    try:
        from django_celery_beat.models import PeriodicTask, CrontabSchedule

        # 1. Clean up old duplicate task name from settings.py if present
        PeriodicTask.objects.using(using_db).filter(name="nightly-db-to-neon-backup").delete()

        # 2. Ensure 2:00 AM UTC crontab schedule exists
        schedule, _ = CrontabSchedule.objects.using(using_db).get_or_create(
            minute="0",
            hour="2",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone="UTC",
        )

        # 3. Ensure Nightly Backup task exists
        task, created = PeriodicTask.objects.using(using_db).get_or_create(
            name="Nightly DB to Neon PostgreSQL Backup",
            defaults={
                "task": "backup.mysql_to_neon",
                "crontab": schedule,
                "enabled": True,
                "description": "Automatically backs up active DB (SQLite/MySQL) to Neon PostgreSQL every night at 2:00 AM UTC",
            },
        )

        if created:
            logger.info("[BACKUP] Auto-created periodic task: Nightly DB to Neon PostgreSQL Backup")

    except Exception as exc:
        logger.warning(f"[BACKUP] Skipping periodic task creation: {exc}")


class BackupConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.backup"
    verbose_name = "DB Backup"

    def ready(self):
        post_migrate.connect(ensure_periodic_tasks, sender=self)
