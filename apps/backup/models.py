from django.db import models


class BackupLog(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        ERROR   = "error",   "Error"
        RUNNING = "running", "Running"

    triggered_at = models.DateTimeField(auto_now_add=True)
    finished_at  = models.DateTimeField(null=True, blank=True)
    status       = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    source_db    = models.CharField(max_length=50, blank=True)
    tables_backed_up = models.IntegerField(default=0)
    total_rows   = models.IntegerField(default=0)
    skipped_tables = models.IntegerField(default=0)
    error_detail = models.JSONField(default=list, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-triggered_at"]
        verbose_name = "Backup Log"
        verbose_name_plural = "Backup Logs"

    def __str__(self):
        return f"{self.triggered_at:%Y-%m-%d %H:%M} | {self.status} | {self.tables_backed_up} tables"

    @property
    def duration_seconds(self):
        if self.finished_at and self.triggered_at:
            return (self.finished_at - self.triggered_at).seconds
        return None
