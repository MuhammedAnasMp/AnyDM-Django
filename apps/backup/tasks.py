"""
Nightly DB → Neon PostgreSQL backup task.
Runs via Celery Beat every night at 2:00 AM UTC.

Auto-detects source DB:
  - SQLite  (local dev)
  - MySQL   (Docker production)
  - PostgreSQL (if switching)

To run manually (local test):
  python -c "
  import os, django
  os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
  django.setup()
  from apps.backup.tasks import backup_mysql_to_neon
  print(backup_mysql_to_neon())
  "
"""

import logging
import os
from celery import shared_task
from django.db import connections
from django.utils import timezone

logger = logging.getLogger(__name__)

# Read backup DB URL from environment (set BACKUP_DATABASE_URL in .env)
NEON_PG_URL = os.environ.get("BACKUP_DATABASE_URL", "")

# Tables to SKIP during backup (only transient task result caches)
SKIP_TABLES = {
    "django_celery_results_taskresult",
    "django_celery_results_chordcounter",
    "django_celery_results_groupresult",
}


def _get_db_engine():
    """Returns: 'sqlite' | 'mysql' | 'postgresql'"""
    engine = connections["default"].settings_dict.get("ENGINE", "")
    if "sqlite"     in engine: return "sqlite"
    if "mysql"      in engine: return "mysql"
    if "postgresql" in engine: return "postgresql"
    return "unknown"


def _list_tables(cursor, engine):
    """Returns list of all table names for the active DB."""
    if engine == "sqlite":
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    elif engine == "mysql":
        cursor.execute("SHOW TABLES")
    elif engine == "postgresql":
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    else:
        return []
    return [row[0] for row in cursor.fetchall()]


def _quote(engine, table):
    """Properly quotes table name per DB dialect."""
    return f"`{table}`" if engine == "mysql" else f'"{table}"'


@shared_task(bind=True, name="backup.mysql_to_neon")
def backup_mysql_to_neon(self):
    """
    Full backup: active DB (SQLite / MySQL / PG) → Neon PostgreSQL.
    Auto-detects source DB type — works locally (SQLite) and in
    production (MySQL Docker) without any code change.
    """
    if not NEON_PG_URL:
        logger.error("[BACKUP] BACKUP_DATABASE_URL not set in .env — aborting.")
        return {"status": "error", "reason": "BACKUP_DATABASE_URL not configured"}

    try:
        import psycopg
        has_psycopg3 = True
    except ImportError:
        try:
            import psycopg2 as psycopg
            has_psycopg3 = False
        except ImportError:
            logger.error("[BACKUP] Neither psycopg nor psycopg2 installed")
            return {"status": "error", "reason": "PostgreSQL driver (psycopg/psycopg2) not installed"}

    engine = _get_db_engine()
    logger.info(f"[BACKUP] Source: {engine.upper()} → Neon PostgreSQL (Singapore)")

    # Create a log entry immediately so admin shows "Running"
    from .models import BackupLog
    log = BackupLog.objects.create(
        status=BackupLog.Status.RUNNING,
        source_db=engine,
        celery_task_id=self.request.id or "",
    )

    stats = {"tables": 0, "rows": 0, "skipped": 0, "errors": []}

    try:
        if has_psycopg3:
            neon = psycopg.connect(NEON_PG_URL, autocommit=False)
        else:
            neon = psycopg.connect(NEON_PG_URL)
            neon.autocommit = False
    except Exception as e:
        logger.error(f"[BACKUP] Neon connection failed: {e}")
        return {"status": "error", "reason": str(e)}

    src_conn = connections["default"]

    try:
        with src_conn.cursor() as cur:
            all_tables = _list_tables(cur, engine)

        logger.info(f"[BACKUP] Found {len(all_tables)} tables to process")

        for table in all_tables:
            if table in SKIP_TABLES:
                stats["skipped"] += 1
                continue

            try:
                quoted = _quote(engine, table)

                with src_conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {quoted} LIMIT 0")
                    cols = [desc[0] for desc in cur.description]
                    cur.execute(f"SELECT * FROM {quoted}")
                    rows = cur.fetchall()

                with neon.cursor() as pg_cur:
                    _ensure_pg_table(pg_cur, table, cols)
                    pg_cur.execute(
                        f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'
                    )
                    if rows:
                        col_list     = ", ".join(f'"{c}"' for c in cols)
                        placeholders = ", ".join(["%s"] * len(cols))
                        # Cast all values to str (safe for TEXT columns)
                        safe_rows = [
                            tuple(str(v) if v is not None else None for v in row)
                            for row in rows
                        ]
                        pg_cur.executemany(
                            f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                            safe_rows
                        )

                neon.commit()
                stats["tables"] += 1
                stats["rows"]   += len(rows)
                logger.info(f"[BACKUP]   {table}: {len(rows)} rows OK")

            except Exception as e:
                neon.rollback()
                stats["errors"].append({"table": table, "error": str(e)})
                logger.warning(f"[BACKUP]   SKIP {table}: {e}")

    finally:
        neon.close()

    status = "success" if not stats["errors"] else "partial"

    # Update log entry with final result
    log.status           = status
    log.finished_at      = timezone.now()
    log.tables_backed_up = stats["tables"]
    log.total_rows       = stats["rows"]
    log.skipped_tables   = stats["skipped"]
    log.error_detail     = stats["errors"]
    log.save()

    logger.info(
        f"[BACKUP] Done | status={status} | "
        f"tables={stats['tables']} | rows={stats['rows']} | "
        f"skipped={stats['skipped']} | errors={len(stats['errors'])}"
    )
    return {
        "status":           status,
        "source_db":        engine,
        "tables_backed_up": stats["tables"],
        "total_rows":       stats["rows"],
        "skipped_tables":   stats["skipped"],
        "errors":           stats["errors"],
    }


def _ensure_pg_table(pg_cur, table, cols):
    """Creates the table in Neon PG if it doesn't exist (TEXT columns, safe fallback)."""
    col_defs = ['"id" BIGINT PRIMARY KEY' if c == "id" else f'"{c}" TEXT' for c in cols]
    pg_cur.execute(f"""
        CREATE TABLE IF NOT EXISTS "{table}" (
            {", ".join(col_defs)}
        )
    """)
