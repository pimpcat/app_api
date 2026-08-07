"""Retención de versiones Data Refresh (últimas N copias restaurables)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from auth.users import ADMIN_SCHEMA
from database import get_db
from tables import SCHEMA

logger = logging.getLogger(__name__)

VERSION_KEEP = 3
_ENSURED = False
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$", re.I)


def ensure_versions_schema() -> None:
    global _ENSURED
    if _ENSURED:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ADMIN_SCHEMA}")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {ADMIN_SCHEMA}.data_refresh_versions (
                  id            BIGSERIAL PRIMARY KEY,
                  table_name    TEXT NOT NULL,
                  backup_table  TEXT NOT NULL UNIQUE,
                  job_id        TEXT,
                  kind          TEXT NOT NULL DEFAULT 'spatial',
                  row_count     BIGINT,
                  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_dr_versions_table_created
                  ON {ADMIN_SCHEMA}.data_refresh_versions (table_name, created_at DESC)
                """
            )
        conn.commit()
    _ENSURED = True


def version_backup_name(table_name: str, job_id: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    j8 = re.sub(r"[^a-zA-Z0-9]", "", job_id)[:8] or "job"
    base = f"{table_name}__dr_ver_{day}_{j8}"
    return base[:63]


def _qident(name: str) -> str:
    n = (name or "").strip()
    if not _IDENT.match(n):
        raise ValueError(f"Identificador inválido: {name!r}")
    return n


def _table_exists(schema: str, table: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM information_schema.tables
                 WHERE table_schema = %s AND table_name = %s
                """,
                (schema, table),
            )
            return cur.fetchone() is not None


def _count_rows(schema: str, table: str) -> Optional[int]:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) AS n FROM "{schema}"."{table}"')
                row = cur.fetchone()
                return int(row["n"]) if row else None
    except Exception:
        return None


def register_version(
    *,
    table_name: str,
    backup_table: str,
    job_id: Optional[str],
    kind: str = "spatial",
    row_count: Optional[int] = None,
) -> Dict[str, Any]:
    ensure_versions_schema()
    if row_count is None:
        row_count = _count_rows(SCHEMA, backup_table)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {ADMIN_SCHEMA}.data_refresh_versions
                  (table_name, backup_table, job_id, kind, row_count)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, table_name, backup_table, job_id, kind, row_count, created_at
                """,
                (table_name, backup_table, job_id, kind, row_count),
            )
            row = cur.fetchone()
        conn.commit()
    prune_old_versions(table_name, keep=VERSION_KEEP)
    return _row_out(row)


def prune_old_versions(table_name: str, keep: int = VERSION_KEEP) -> List[str]:
    """DROP backups más allá de `keep`. Retorna nombres eliminados."""
    ensure_versions_schema()
    dropped: List[str] = []
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, backup_table
                  FROM {ADMIN_SCHEMA}.data_refresh_versions
                 WHERE table_name = %s
                 ORDER BY created_at DESC
                """,
                (table_name,),
            )
            rows = cur.fetchall() or []
            excess = rows[keep:]
            for r in excess:
                bak = r["backup_table"]
                try:
                    cur.execute(f'DROP TABLE IF EXISTS "{SCHEMA}"."{bak}" CASCADE')
                except Exception as exc:
                    logger.warning("No se pudo DROP versión %s: %s", bak, exc)
                cur.execute(
                    f"DELETE FROM {ADMIN_SCHEMA}.data_refresh_versions WHERE id = %s",
                    (r["id"],),
                )
                dropped.append(bak)
        conn.commit()
    return dropped


def list_versions(table_name: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_versions_schema()
    lim = max(1, min(int(limit or 50), 100))
    with get_db() as conn:
        with conn.cursor() as cur:
            if table_name:
                cur.execute(
                    f"""
                    SELECT id, table_name, backup_table, job_id, kind, row_count, created_at
                      FROM {ADMIN_SCHEMA}.data_refresh_versions
                     WHERE table_name = %s
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (table_name, lim),
                )
            else:
                cur.execute(
                    f"""
                    SELECT id, table_name, backup_table, job_id, kind, row_count, created_at
                      FROM {ADMIN_SCHEMA}.data_refresh_versions
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (lim,),
                )
            rows = cur.fetchall() or []
    return [_row_out(r) for r in rows]


def get_version(version_id: int) -> Optional[Dict[str, Any]]:
    ensure_versions_schema()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, table_name, backup_table, job_id, kind, row_count, created_at
                  FROM {ADMIN_SCHEMA}.data_refresh_versions
                 WHERE id = %s
                """,
                (int(version_id),),
            )
            row = cur.fetchone()
    return _row_out(row) if row else None


def restore_version(version_id: int, *, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Swap producción ↔ backup versionado; registra la prod actual como nueva versión."""
    from data_refresh.jobs_store import new_job_id

    ver = get_version(version_id)
    if not ver:
        raise ValueError("VERSION_NOT_FOUND")
    table = _qident(ver["table_name"])
    bak = _qident(ver["backup_table"])
    if not _table_exists(SCHEMA, bak):
        raise ValueError(f"BACKUP_MISSING:{bak}")
    if not _table_exists(SCHEMA, table):
        raise ValueError(f"TARGET_NOT_FOUND:{table}")

    tmp = f"{table}__dr_swap_{new_job_id()[:8]}"[:63]
    kind = ver.get("kind") or "spatial"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '60s'")
            cur.execute("SET LOCAL statement_timeout = '300s'")
            cur.execute(f'DROP TABLE IF EXISTS "{SCHEMA}"."{tmp}" CASCADE')
            cur.execute(f'ALTER TABLE "{SCHEMA}"."{table}" RENAME TO "{tmp}"')
            cur.execute(f'ALTER TABLE "{SCHEMA}"."{bak}" RENAME TO "{table}"')
            # El backup usado ya no existe con ese nombre: actualizar registro
            cur.execute(
                f"DELETE FROM {ADMIN_SCHEMA}.data_refresh_versions WHERE id = %s",
                (int(version_id),),
            )
            # Conservar la producción anterior como nueva versión
            new_bak = version_backup_name(table, new_job_id())
            cur.execute(f'ALTER TABLE "{SCHEMA}"."{tmp}" RENAME TO "{new_bak}"')
            cur.execute(f'ANALYZE "{SCHEMA}"."{table}"')
        conn.commit()

    row_count = _count_rows(SCHEMA, new_bak)
    registered = register_version(
        table_name=table,
        backup_table=new_bak,
        job_id=ver.get("job_id"),
        kind=kind,
        row_count=row_count,
    )

    martin = None
    if kind == "spatial":
        try:
            from data_refresh.service import _wait_martin_soft

            martin = _wait_martin_soft(table, timeout_s=20)
        except Exception as exc:
            martin = {"ok": False, "error": str(exc)[:200]}

    if user_id is not None:
        try:
            from visor_catalog_admin_service import record_audit

            record_audit(
                int(user_id),
                "data_refresh_restore",
                table,
                {"version_id": version_id, "from_backup": bak},
                {"restored_table": table, "new_backup": new_bak},
            )
        except Exception:
            pass

    return {
        "ok": True,
        "table_name": table,
        "restored_from": bak,
        "new_version": registered,
        "martin": martin,
    }


def snapshot_table_as_version(
    *,
    table_name: str,
    job_id: str,
    kind: str = "indicator",
) -> Dict[str, Any]:
    """CREATE TABLE copia de producción antes de merge de indicadores."""
    table = _qident(table_name)
    bak = version_backup_name(table, job_id)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{SCHEMA}"."{bak}" CASCADE')
            cur.execute(
                f'CREATE TABLE "{SCHEMA}"."{bak}" AS TABLE "{SCHEMA}"."{table}"'
            )
        conn.commit()
    return register_version(
        table_name=table,
        backup_table=bak,
        job_id=job_id,
        kind=kind,
        row_count=_count_rows(SCHEMA, bak),
    )


def _row_out(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {}
    created = row.get("created_at")
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_s = created.isoformat()
    else:
        created_s = str(created) if created else None
    return {
        "id": row["id"],
        "table_name": row["table_name"],
        "backup_table": row["backup_table"],
        "job_id": row.get("job_id"),
        "kind": row.get("kind") or "spatial",
        "row_count": row.get("row_count"),
        "created_at": created_s,
    }
