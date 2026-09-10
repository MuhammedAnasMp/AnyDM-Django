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

# Tables to SKIP during backup (set to empty so 100% of tables are backed up)
SKIP_TABLES = set()


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
    logger.info(f"[BACKUP] Source: {engine.upper()} → Target: {NEON_PG_URL[:30]}...")

    # Create a log entry immediately so admin shows "Running"
    from .models import BackupLog
    log = BackupLog.objects.create(
        status=BackupLog.Status.RUNNING,
        source_db=engine,
        celery_task_id=self.request.id or "",
    )

    stats = {"tables": 0, "rows": 0, "skipped": 0, "errors": []}

    try:
        target_conn, target_engine = _connect_target_db(NEON_PG_URL)
    except Exception as e:
        logger.error(f"[BACKUP] Connection failed: {e}")
        log.status = BackupLog.Status.ERROR
        log.finished_at = timezone.now()
        log.error_detail = [{"error": f"Failed to connect to BACKUP_DATABASE_URL: {e}"}]
        log.save()
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

                if target_engine == "sqlite":
                    target_cur = target_conn.cursor()
                    _ensure_sqlite_table(target_cur, table, cols, rows)
                    if rows:
                        col_list     = ", ".join(f'`{c}`' for c in cols)
                        placeholders = ", ".join(["?"] * len(cols))
                        safe_rows = [
                            tuple(str(v) if v is not None else None for v in row)
                            for row in rows
                        ]
                        target_cur.executemany(
                            f'INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})',
                            safe_rows
                        )
                elif target_engine == "postgresql":
                    with target_conn.cursor() as target_cur:
                        _ensure_pg_table(target_cur, table, cols, rows)
                        if rows:
                            col_list     = ", ".join(f'"{c}"' for c in cols)
                            placeholders = ", ".join(["%s"] * len(cols))
                            
                            numeric_cols = set()
                            boolean_cols = set()
                            timestamp_cols = set()
                            for col_idx, c in enumerate(cols):
                                col_type = _detect_col_type(c, [r[col_idx] for r in rows])
                                if "BIGINT" in col_type:
                                    numeric_cols.add(col_idx)
                                elif "BOOLEAN" in col_type:
                                    boolean_cols.add(col_idx)
                                elif "TIMESTAMPTZ" in col_type:
                                    timestamp_cols.add(col_idx)

                            safe_rows = []
                            for row in rows:
                                formatted_row = []
                                for col_idx, v in enumerate(row):
                                    if v is None or v == "":
                                        formatted_row.append(None)
                                    elif col_idx in boolean_cols:
                                        formatted_row.append(True if str(v).strip().lower() in ("true", "1", "t") else False)
                                    elif col_idx in timestamp_cols:
                                        formatted_row.append(str(v).strip())
                                    elif col_idx in numeric_cols:
                                        val_str = str(v).strip().lstrip("-")
                                        formatted_row.append(int(v) if val_str.isdigit() else None)
                                    else:
                                        formatted_row.append(str(v))
                                safe_rows.append(tuple(formatted_row))

                            target_cur.executemany(
                                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                                safe_rows
                            )
                            if "id" in cols:
                                target_cur.execute(f"""
                                    SELECT setval(pg_get_serial_sequence('"{table}"', 'id'), COALESCE(MAX("id"), 1)) FROM "{table}"
                                """)
                else:
                    # MySQL target
                    with target_conn.cursor() as target_cur:
                        _ensure_mysql_table(target_cur, table, cols, rows)
                        if rows:
                            col_list     = ", ".join(f'`{c}`' for c in cols)
                            placeholders = ", ".join(["%s"] * len(cols))

                            numeric_cols = set()
                            boolean_cols = set()
                            timestamp_cols = set()
                            for col_idx, c in enumerate(cols):
                                col_type = _detect_col_type(c, [r[col_idx] for r in rows])
                                if "BIGINT" in col_type:
                                    numeric_cols.add(col_idx)
                                elif "BOOLEAN" in col_type:
                                    boolean_cols.add(col_idx)
                                elif "TIMESTAMPTZ" in col_type:
                                    timestamp_cols.add(col_idx)

                            safe_rows = []
                            for row in rows:
                                formatted_row = []
                                for col_idx, v in enumerate(row):
                                    if v is None or v == "":
                                        formatted_row.append(None)
                                    elif col_idx in boolean_cols:
                                        formatted_row.append(1 if str(v).strip().lower() in ("true", "1", "t") else 0)
                                    elif col_idx in timestamp_cols:
                                        formatted_row.append(str(v).strip()[:19])
                                    elif col_idx in numeric_cols:
                                        val_str = str(v).strip().lstrip("-")
                                        formatted_row.append(int(v) if val_str.isdigit() else None)
                                    else:
                                        formatted_row.append(str(v))
                                safe_rows.append(tuple(formatted_row))

                            target_cur.executemany(
                                f'INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})',
                                safe_rows
                            )

                target_conn.commit()
                stats["tables"] += 1
                stats["rows"]   += len(rows)
                logger.info(f"[BACKUP]   {table}: {len(rows)} rows OK")

            except Exception as e:
                target_conn.rollback()
                stats["errors"].append({"table": table, "error": str(e)})
                logger.warning(f"[BACKUP]   SKIP {table}: {e}")

    finally:
        target_conn.close()

    status = "success" if not stats["errors"] else "partial"

    # Update log entry with final result
    log.status           = status
    log.finished_at      = timezone.now()
    log.tables_backed_up = stats["tables"]
    log.total_rows       = stats["rows"]
    log.skipped_tables   = stats["skipped"]
    log.error_detail     = stats["errors"]
    log.save()

    # Purge backup logs older than 10 days
    from datetime import timedelta
    cutoff = timezone.now() - timedelta(days=10)
    deleted_count, _ = BackupLog.objects.filter(triggered_at__lt=cutoff).delete()
    if deleted_count:
        logger.info(f"[BACKUP] Purged {deleted_count} backup log(s) older than 10 days")

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


def _detect_col_type(col_name, sample_values):
    """Determines PostgreSQL column type (BIGINT, BOOLEAN, TIMESTAMPTZ, TEXT)."""
    if col_name == "id":
        return "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"

    # Known Boolean columns
    if col_name in ("enabled", "one_off", "is_active", "is_staff", "is_superuser", "is_enabled", "is_used", "expired", "verified") or (col_name.startswith(("is_", "has_")) and not col_name.endswith(("_count", "_id", "_seconds", "_bytes"))):
        return "BOOLEAN"

    non_nulls = [v for v in sample_values if v is not None and str(v).strip() != ""]
    if not non_nulls:
        if (col_name.endswith(("_at", "_date", "_time")) or col_name in ("date", "datetime", "timestamp", "expire_date", "created_at", "updated_at", "last_login", "date_joined", "expires_at", "clocked_time", "last_run_at")) and col_name not in ("prompt_at",):
            return "TIMESTAMPTZ"
        if col_name.endswith("_id") and col_name not in ("object_id", "instagram_message_id", "message_id", "thread_id", "interaction_id", "order_id", "account_id", "action_id"):
            return "BIGINT"
        return "TEXT"

    # Check if all non-null values are boolean
    if all(isinstance(v, bool) or str(v).lower() in ("true", "false") for v in non_nulls):
        return "BOOLEAN"

    # Check if non-null values are valid ISO timestamp strings (YYYY-MM-DD...)
    def _is_iso_date(v):
        s = str(v).strip()
        return len(s) >= 10 and s[:4].isdigit() and s[4] == "-" and s[7] == "-"

    if all(_is_iso_date(v) for v in non_nulls):
        return "TIMESTAMPTZ"

    # Check if all non-null values are numeric integers
    all_is_digit = True
    for v in non_nulls:
        if isinstance(v, bool):
            all_is_digit = False
            break
        if isinstance(v, int):
            continue
        val_str = str(v).strip()
        if not (val_str.isdigit() or (val_str.startswith("-") and val_str[1:].isdigit())):
            all_is_digit = False
            break

    if all_is_digit:
        return "BIGINT"
    return "TEXT"


def _ensure_pg_table(pg_cur, table, cols, rows):
    """Recreates the table in Neon PG using dynamic column type detection."""
    pg_cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    col_defs = []
    for col_idx, c in enumerate(cols):
        col_vals = [r[col_idx] for r in rows] if rows else []
        col_type = _detect_col_type(c, col_vals)
        col_defs.append(f'"{c}" {col_type}')

    pg_cur.execute(f"""
        CREATE TABLE "{table}" (
            {", ".join(col_defs)}
        )
    """)


def _ensure_mysql_table(mysql_cur, table, cols, rows):
    """Recreates table in MySQL target DB with proper types."""
    mysql_cur.execute("SET FOREIGN_KEY_CHECKS = 0;")
    mysql_cur.execute(f"DROP TABLE IF EXISTS `{table}`")
    col_defs = []
    for col_idx, c in enumerate(cols):
        col_vals = [r[col_idx] for r in rows] if rows else []
        col_type = _detect_col_type(c, col_vals)
        if c == "id":
            col_defs.append("`id` BIGINT AUTO_INCREMENT PRIMARY KEY")
        elif "BOOLEAN" in col_type:
            col_defs.append(f"`{c}` TINYINT(1) NULL")
        elif "BIGINT" in col_type:
            col_defs.append(f"`{c}` BIGINT NULL")
        elif "TIMESTAMPTZ" in col_type:
            col_defs.append(f"`{c}` DATETIME NULL")
        else:
            col_defs.append(f"`{c}` LONGTEXT NULL")

    mysql_cur.execute(f"CREATE TABLE `{table}` ({', '.join(col_defs)}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;")
    mysql_cur.execute("SET FOREIGN_KEY_CHECKS = 1;")


def _ensure_sqlite_table(sqlite_cur, table, cols, rows):
    """Recreates table in SQLite target DB."""
    sqlite_cur.execute(f"DROP TABLE IF EXISTS `{table}`")
    col_defs = []
    for c in cols:
        if c == "id":
            col_defs.append("`id` INTEGER PRIMARY KEY AUTOINCREMENT")
        else:
            col_defs.append(f"`{c}` TEXT NULL")
    sqlite_cur.execute(f"CREATE TABLE `{table}` ({', '.join(col_defs)})")


def _connect_target_db(target_url):
    """
    Connects to target DB (PostgreSQL, MySQL, or SQLite) from target_url.
    Returns: (target_conn, target_engine_type)
    """
    import dj_database_url
    config = dj_database_url.parse(target_url)
    engine = config.get("ENGINE", "")

    if "postgresql" in engine or target_url.startswith("postgres"):
        try:
            import psycopg
            conn = psycopg.connect(target_url, autocommit=False)
        except ImportError:
            import psycopg2 as psycopg
            conn = psycopg.connect(target_url)
            conn.autocommit = False
        return conn, "postgresql"
    elif "mysql" in engine or target_url.startswith("mysql"):
        import MySQLdb
        conn = MySQLdb.connect(
            host=config.get("HOST") or "127.0.0.1",
            port=int(config.get("PORT") or 3306),
            user=config.get("USER") or "root",
            passwd=config.get("PASSWORD") or "",
            db=config.get("NAME") or "",
            charset="utf8mb4",
        )
        conn.autocommit(False)
        return conn, "mysql"
    elif "sqlite" in engine or target_url.startswith("sqlite"):
        import sqlite3
        db_path = config.get("NAME") or target_url.replace("sqlite:///", "")
        conn = sqlite3.connect(db_path)
        return conn, "sqlite"
    else:
        raise ValueError(f"Unsupported backup DB engine in URL: {target_url}")
