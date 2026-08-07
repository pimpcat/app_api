"""Persistencia de jobs Data Refresh en atlas_admin."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from auth.users import ADMIN_SCHEMA
from database import get_db

_ENSURED = False


def ensure_jobs_schema() -> None:
    global _ENSURED
    if _ENSURED:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ADMIN_SCHEMA}")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {ADMIN_SCHEMA}.data_refresh_jobs (
                  id            TEXT PRIMARY KEY,
                  user_id       INT,
                  target_table  TEXT NOT NULL,
                  staging_table TEXT,
                  status        TEXT NOT NULL,
                  report        JSONB,
                  error_message TEXT,
                  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_data_refresh_jobs_created
                  ON {ADMIN_SCHEMA}.data_refresh_jobs (created_at DESC)
                """
            )
        conn.commit()
    _ENSURED = True


def new_job_id() -> str:
    return secrets.token_hex(12)


def insert_job(
    *,
    job_id: str,
    user_id: Optional[int],
    target_table: str,
    staging_table: str,
    status: str,
    report: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    ensure_jobs_schema()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {ADMIN_SCHEMA}.data_refresh_jobs
                  (id, user_id, target_table, staging_table, status, report, error_message)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    job_id,
                    user_id,
                    target_table,
                    staging_table,
                    status,
                    json.dumps(report or {}, ensure_ascii=False),
                    error_message,
                ),
            )
        conn.commit()


def update_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    report: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    staging_table: Optional[str] = None,
) -> None:
    ensure_jobs_schema()
    sets = ["updated_at = NOW()"]
    params: List[Any] = []
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if report is not None:
        sets.append("report = %s::jsonb")
        params.append(json.dumps(report, ensure_ascii=False))
    if error_message is not None:
        sets.append("error_message = %s")
        params.append(error_message)
    if staging_table is not None:
        sets.append("staging_table = %s")
        params.append(staging_table)
    params.append(job_id)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {ADMIN_SCHEMA}.data_refresh_jobs
                   SET {", ".join(sets)}
                 WHERE id = %s
                """,
                params,
            )
        conn.commit()


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    ensure_jobs_schema()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, user_id, target_table, staging_table, status, report,
                       error_message, created_at, updated_at
                  FROM {ADMIN_SCHEMA}.data_refresh_jobs
                 WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    report = row.get("report")
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except json.JSONDecodeError:
            report = {}
    return {
        "id": row["id"],
        "user_id": row.get("user_id"),
        "target_table": row["target_table"],
        "staging_table": row.get("staging_table"),
        "status": row["status"],
        "report": report or {},
        "error_message": row.get("error_message"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def list_recent_jobs(limit: int = 20) -> List[Dict[str, Any]]:
    ensure_jobs_schema()
    lim = max(1, min(int(limit or 20), 100))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, target_table, status, created_at, updated_at, error_message
                  FROM {ADMIN_SCHEMA}.data_refresh_jobs
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (lim,),
            )
            rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "target_table": r["target_table"],
            "status": r["status"],
            "created_at": _iso(r.get("created_at")),
            "updated_at": _iso(r.get("updated_at")),
            "error_message": r.get("error_message"),
        }
        for r in rows
    ]


def _job_kind(report: Any) -> str:
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except json.JSONDecodeError:
            report = {}
    if not isinstance(report, dict):
        return "spatial"
    if report.get("kind") == "indicator":
        return "indicator"
    return "spatial"


def _job_summary(report: Dict[str, Any], kind: str, status: str) -> str:
    if kind == "indicator":
        label = report.get("template_label") or report.get("template_id") or ""
        matched = report.get("entities_matched")
        if matched is None:
            matched = report.get("municipal_matched")
        parts = []
        if label:
            parts.append(str(label))
        if matched is not None:
            scope = report.get("scope") or ""
            unit = "ents" if scope == "nacional" else "mun"
            parts.append(f"{matched} {unit}")
        apply_s = report.get("apply_summary") or {}
        if status == "applied" and apply_s.get("columns_updated"):
            parts.append(f"{len(apply_s['columns_updated'])} cols")
        return " · ".join(parts) if parts else "Indicador"
    # spatial
    parts = []
    fc = report.get("final_count")
    if fc is None:
        fc = report.get("staging_count") or report.get("production_count")
    if fc is not None:
        parts.append(f"{int(fc):,} registros".replace(",", " "))
    geom = report.get("geometry_staging") or report.get("geometry_production")
    if geom:
        parts.append(str(geom).upper())
    return " · ".join(parts) if parts else "Capa espacial"


def _elapsed_seconds(report: Dict[str, Any], created_at: Any, updated_at: Any) -> Optional[float]:
    apply_s = report.get("apply_summary") or {}
    if apply_s.get("elapsed_seconds") is not None:
        try:
            return float(apply_s["elapsed_seconds"])
        except (TypeError, ValueError):
            pass
    if report.get("elapsed_seconds") is not None:
        try:
            return float(report["elapsed_seconds"])
        except (TypeError, ValueError):
            pass
    try:
        if created_at and updated_at:
            c = created_at if isinstance(created_at, datetime) else None
            u = updated_at if isinstance(updated_at, datetime) else None
            if c and u:
                return round(max(0.0, (u - c).total_seconds()), 1)
    except Exception:
        pass
    return None


def list_history_jobs(
    *,
    limit: int = 50,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    target_table: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Historial enriquecido para UI (fecha, usuario, tipo, duración, resumen)."""
    ensure_jobs_schema()
    lim = max(1, min(int(limit or 50), 100))
    where = ["1=1"]
    params: List[Any] = []
    if status:
        where.append("j.status = %s")
        params.append(status.strip())
    if target_table:
        where.append("j.target_table = %s")
        params.append(target_table.strip())
    if kind in ("spatial", "indicator"):
        if kind == "indicator":
            where.append("COALESCE(j.report->>'kind','') = 'indicator'")
        else:
            where.append("COALESCE(j.report->>'kind','') IS DISTINCT FROM 'indicator'")
    params.append(lim)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT j.id, j.user_id, j.target_table, j.staging_table, j.status,
                       j.report, j.error_message, j.created_at, j.updated_at,
                       u.username
                  FROM {ADMIN_SCHEMA}.data_refresh_jobs j
                  LEFT JOIN {ADMIN_SCHEMA}.users u ON u.id = j.user_id
                 WHERE {" AND ".join(where)}
                 ORDER BY j.created_at DESC
                 LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall() or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        report = r.get("report")
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except json.JSONDecodeError:
                report = {}
        report = report or {}
        k = _job_kind(report)
        out.append(
            {
                "id": r["id"],
                "user_id": r.get("user_id"),
                "username": r.get("username") or (f"user#{r['user_id']}" if r.get("user_id") else "—"),
                "target_table": r["target_table"],
                "status": r["status"],
                "kind": k,
                "kind_label": "Indicator Refresh" if k == "indicator" else "Spatial Refresh",
                "summary": _job_summary(report, k, r["status"]),
                "elapsed_seconds": _elapsed_seconds(
                    report, r.get("created_at"), r.get("updated_at")
                ),
                "template_label": report.get("template_label"),
                "created_at": _iso(r.get("created_at")),
                "updated_at": _iso(r.get("updated_at")),
                "error_message": r.get("error_message"),
            }
        )
    return out


def _iso(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat()
    return str(val)
