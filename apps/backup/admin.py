from django.contrib import admin, messages
from django.utils.html import format_html
from django.urls import path
from django.shortcuts import redirect
from .models import BackupLog


@admin.register(BackupLog)
class BackupLogAdmin(admin.ModelAdmin):
    list_display  = (
        "triggered_at", "status_badge", "source_db",
        "tables_backed_up", "total_rows", "skipped_tables",
        "duration_display", "celery_task_id",
    )
    list_filter   = ("status", "source_db")
    readonly_fields = (
        "triggered_at", "finished_at", "status", "source_db",
        "tables_backed_up", "total_rows", "skipped_tables",
        "error_detail", "celery_task_id",
    )
    ordering      = ("-triggered_at",)
    list_per_page = 20

    # ── "Run Backup Now" button in the changelist header ──────────────────
    change_list_template = "admin/backup/backuplog/change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "run-now/",
                self.admin_site.admin_view(self.run_backup_now),
                name="backup_run_now",
            )
        ]
        return custom + urls

    def run_backup_now(self, request):
        from .tasks import backup_mysql_to_neon
        task = backup_mysql_to_neon.delay()
        self.message_user(
            request,
            f"Backup task queued! Task ID: {task.id}",
            messages.SUCCESS,
        )
        return redirect("../")

    # ── Custom display columns ─────────────────────────────────────────────
    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "success": "#28a745",
            "partial": "#fd7e14",
            "error":   "#dc3545",
            "running": "#007bff",
        }
        color = colors.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 10px;'
            'border-radius:12px;font-size:12px;font-weight:600">{}</span>',
            color, obj.get_status_display()
        )

    @admin.display(description="Duration")
    def duration_display(self, obj):
        d = obj.duration_seconds
        if d is None:
            return "—"
        return f"{d}s"
