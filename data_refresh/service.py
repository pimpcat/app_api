"""Servicio Data Refresh: targets, staging SHP, diff, swap."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from data_refresh.jobs_store import (
    get_job,
    insert_job,
    list_recent_jobs,
    new_job_id,
    update_job,
)
from data_refresh.names import (
    BLOCKED_REFRESH_TARGETS,
    STAGING_SCHEMA,
    assert_job_id,
    assert_table_name,
    staging_table_name,
)
from database import get_db
from tables import SCHEMA
from visor_catalog_admin_service import record_audit
from visor_shp_import import (
    _extract_shp_dir,
    _feature_count,
    _find_shp_file,
    _geometry_type,
    _geometry_type_detail,
    _list_columns,
    _ogr2ogr_import,
    _table_exists,
)

logger = logging.getLogger(__name__)

# Directorios de trabajo por job (pipeline async).
_JOB_WORKDIRS: Dict[str, Path] = {}


def ensure_staging_schema() -> None:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {STAGING_SCHEMA}")
        conn.commit()


def list_targets() -> List[Dict[str, Any]]:
    """Tablas atlas con geometría (candidatas a refresh).

    Usa ``pg_class.reltuples`` (estimado) para no bloquear con COUNT(*) exacto
    si alguna tabla grande está bajo lock (p. ej. swap a medias).
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '15s'")
            cur.execute(
                """
                SELECT g.f_table_name AS table_name,
                       g.type AS geom_type,
                       g.srid,
                       COALESCE(
                         (
                           SELECT GREATEST(c.reltuples, 0)::bigint
                             FROM pg_class c
                             JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname = g.f_table_schema
                              AND c.relname = g.f_table_name
                            LIMIT 1
                         ),
                         0
                       ) AS est_count
                  FROM geometry_columns g
                 WHERE g.f_table_schema = %s
                 ORDER BY g.f_table_name
                """,
                (SCHEMA,),
            )
            rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        name = (r.get("table_name") or "").lower()
        if not name or name in BLOCKED_REFRESH_TARGETS:
            continue
        if name.startswith("dr_") or "__dr_" in name:
            continue
        out.append(
            {
                "table": name,
                "geometry": _norm_geom(r.get("geom_type")),
                "srid": r.get("srid"),
                "feature_count": int(r.get("est_count") or 0),
                "feature_count_estimated": True,
                "blocked": False,
            }
        )
    return out


def _norm_geom(gtype: Any) -> str:
    t = str(gtype or "").upper()
    if "LINE" in t:
        return "line"
    if "POLYGON" in t:
        return "polygon"
    return "point"


def meta() -> Dict[str, Any]:
    return {
        "ok": True,
        "version": "1.0.0",
        "staging_schema": STAGING_SCHEMA,
        "production_schema": SCHEMA,
        "formats": [".zip", ".shp"],
        "pipeline": "spatial_shp_swap",
        "blocked_targets": sorted(BLOCKED_REFRESH_TARGETS),
        "recent_jobs": list_recent_jobs(10),
    }


def _drop_table(schema: str, table: str) -> None:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE')
        conn.commit()


def _column_names(schema: str, table: str) -> Set[str]:
    return {c["name"].lower() for c in _list_columns(schema, table)}


# Diff fila-a-fila es caro; el apply hace swap completo de todos modos.
_KEY_DIFF_MAX_FEATURES = 40_000


def _estimate_count(schema: str, table: str) -> int:
    """Conteo estimado (pg_class); no bloquea con locks de lectura pesada."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT GREATEST(c.reltuples, 0)::bigint AS n
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = %s AND c.relname = %s
                 LIMIT 1
                """,
                (schema, table),
            )
            row = cur.fetchone()
    return int(row["n"]) if row else 0


def _safe_count(schema: str, table: str, *, timeout_s: int = 12) -> Dict[str, Any]:
    """COUNT con timeout; si falla o la tabla es grande, usa estimado."""
    est = _estimate_count(schema, table)
    if est > _KEY_DIFF_MAX_FEATURES:
        return {"count": est, "exact": False, "source": "estimate_large"}
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = '{int(timeout_s)}s'")
                cur.execute(f'SELECT COUNT(*) AS n FROM "{schema}"."{table}"')
                row = cur.fetchone() or {}
            conn.commit()
        return {"count": int(row.get("n") or 0), "exact": True, "source": "count"}
    except Exception:
        return {"count": est, "exact": False, "source": "estimate_fallback"}


def _pick_key(prod_cols: Set[str], stg_cols: Set[str]) -> Optional[str]:
    for cand in ("gid", "cve_mun", "cvegeo", "id"):
        if cand in prod_cols and cand in stg_cols:
            return cand
    return None


def _table_srid(schema: str, table: str) -> Optional[int]:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT srid FROM geometry_columns
                     WHERE f_table_schema = %s AND f_table_name = %s
                     LIMIT 1
                    """,
                    (schema, table),
                )
                row = cur.fetchone()
                if row and row.get("srid") is not None:
                    return int(row["srid"])
                cur.execute(
                    "SELECT Find_SRID(%s, %s, 'the_geom') AS srid",
                    (schema, table),
                )
                row = cur.fetchone()
                if row and row.get("srid") is not None and int(row["srid"]) > 0:
                    return int(row["srid"])
    except Exception:
        return None
    return None


def _staging_duplicate_keys(schema: str, table: str, key: str) -> int:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '20s'")
                cur.execute(
                    f"""
                    SELECT COALESCE(SUM(c), 0)::bigint AS dups FROM (
                      SELECT COUNT(*) AS c
                        FROM "{schema}"."{table}"
                       WHERE "{key}" IS NOT NULL
                       GROUP BY "{key}"
                      HAVING COUNT(*) > 1
                    ) t
                    """
                )
                row = cur.fetchone() or {}
            conn.commit()
        return int(row.get("dups") or 0)
    except Exception:
        return 0


def _critical_null_warnings(schema: str, table: str, key: Optional[str]) -> List[str]:
    cols: List[str] = []
    if key:
        cols.append(key)
    try:
        names = _column_names(schema, table)
        for c in ("the_geom", "geom"):
            if c in names:
                cols.append(c)
                break
    except Exception:
        pass
    out: List[str] = []
    for col in cols[:3]:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '15s'")
                    cur.execute(
                        f"""
                        SELECT COUNT(*) AS n FROM "{schema}"."{table}"
                         WHERE "{col}" IS NULL
                        """
                    )
                    row = cur.fetchone() or {}
                conn.commit()
            n = int(row.get("n") or 0)
            if n > 0:
                out.append(f"{n} nulos en «{col}»")
        except Exception:
            continue
    return out


def _has_gist_index(schema: str, table: str) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                      FROM pg_indexes
                     WHERE schemaname = %s AND tablename = %s
                       AND indexdef ILIKE '%%USING gist%%'
                     LIMIT 1
                    """,
                    (schema, table),
                )
                return cur.fetchone() is not None
    except Exception:
        return False


def _key_diff_stats(
    target: str,
    staging: str,
    key: str,
    prod_count: int,
    stg_count: int,
) -> Dict[str, Any]:
    """Compara claves con EXCEPT/INTERSECT. Nunca CREATE INDEX ni ANALYZE aquí."""
    largest = max(prod_count, stg_count)
    base: Dict[str, Any] = {
        "key": key,
        "note": "Conteo por clave; el swap MVP reemplaza la tabla completa.",
    }
    if largest > _KEY_DIFF_MAX_FEATURES:
        base.update(
            {
                "skipped": True,
                "reason": "large_table",
                "note": (
                    f"Diff detallado omitido ({largest:,} feats > {_KEY_DIFF_MAX_FEATURES:,}). "
                    "El swap reemplaza la tabla completa; use production_count / "
                    "staging_count / delta_count."
                ),
            }
        )
        return base

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '20s'")
                cur.execute(
                    f"""
                    SELECT
                      (SELECT COUNT(*) FROM (
                        SELECT t."{key}" FROM "{SCHEMA}"."{target}" t
                        EXCEPT
                        SELECT s."{key}" FROM "{STAGING_SCHEMA}"."{staging}" s
                      ) q) AS only_prod,
                      (SELECT COUNT(*) FROM (
                        SELECT s."{key}" FROM "{STAGING_SCHEMA}"."{staging}" s
                        EXCEPT
                        SELECT t."{key}" FROM "{SCHEMA}"."{target}" t
                      ) q) AS only_stg,
                      (SELECT COUNT(*) FROM (
                        SELECT t."{key}" FROM "{SCHEMA}"."{target}" t
                        INTERSECT
                        SELECT s."{key}" FROM "{STAGING_SCHEMA}"."{staging}" s
                      ) q) AS matched
                    """
                )
                row = cur.fetchone() or {}
            conn.commit()
        base.update(
            {
                "would_delete": int(row.get("only_prod") or 0),
                "would_insert": int(row.get("only_stg") or 0),
                "would_keep_or_update": int(row.get("matched") or 0),
            }
        )
        return base
    except Exception as exc:
        return {
            "key": key,
            "skipped": True,
            "reason": "timeout_or_error",
            "error": str(exc)[:240],
            "note": (
                "Diff por clave omitido (timeout/error). "
                "Conteos y columnas sí; el swap reemplaza la tabla completa."
            ),
        }


def _inspect_point_geometry(schema: str, table: str) -> Dict[str, Any]:
    """Detecta MULTIPOINT y si es seguro colapsar a POINT (1 vértice por fila)."""
    detail = _geometry_type_detail(schema, table)
    pretty = {
        "point": "POINT",
        "multipoint": "MULTIPOINT",
        "line": "LINESTRING",
        "multiline": "MULTILINESTRING",
        "polygon": "POLYGON",
        "multipolygon": "MULTIPOLYGON",
    }.get(detail, detail.upper())

    base: Dict[str, Any] = {
        "original_type": pretty,
        "detail": detail,
        "elements_per_geometry": None,
        "max_parts": None,
        "rows_with_multiple_parts": 0,
        "can_convert_to_point": False,
        "converted": False,
        "conversion_applied": False,
        "final_type": pretty,
        "status": "not_applicable",
        "message": "",
    }

    if detail == "point":
        base.update(
            {
                "status": "already_point",
                "elements_per_geometry": 1,
                "max_parts": 1,
                "message": "La geometría ya es POINT.",
            }
        )
        return base

    if detail != "multipoint":
        base["status"] = "not_point_family"
        base["message"] = f"Tipo {pretty}: no aplica normalización a POINT."
        return base

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '60s'")
                cur.execute(
                    f"""
                    SELECT
                      COUNT(*) FILTER (
                        WHERE the_geom IS NOT NULL AND ST_NumGeometries(the_geom) > 1
                      )::bigint AS multi_rows,
                      COALESCE(MAX(ST_NumGeometries(the_geom)), 0)::int AS max_parts,
                      COUNT(*) FILTER (WHERE the_geom IS NOT NULL)::bigint AS with_geom
                    FROM "{schema}"."{table}"
                    """
                )
                row = cur.fetchone() or {}
            conn.commit()
    except Exception as exc:
        base.update(
            {
                "status": "inspect_error",
                "message": f"No se pudo inspeccionar geometría: {str(exc)[:200]}",
            }
        )
        return base

    multi_rows = int(row.get("multi_rows") or 0)
    max_parts = int(row.get("max_parts") or 0)
    base["max_parts"] = max_parts
    base["rows_with_multiple_parts"] = multi_rows

    if multi_rows > 0 or max_parts > 1:
        base.update(
            {
                "status": "unsafe",
                "can_convert_to_point": False,
                "elements_per_geometry": max_parts,
                "message": (
                    "Conversión automática no posible. "
                    f"Hay geometrías MULTIPOINT con más de un punto "
                    f"({multi_rows:,} filas; máx. {max_parts} partes)."
                ),
            }
        )
        return base

    base.update(
        {
            "status": "convertible",
            "can_convert_to_point": True,
            "elements_per_geometry": 1,
            "message": (
                "MULTIPOINT detectado: todos los registros contienen un único punto. "
                "Se puede convertir automáticamente a POINT."
            ),
        }
    )
    return base


def _convert_table_to_point(schema: str, table: str) -> Dict[str, Any]:
    """ALTER typmod MULTIPOINT → POINT usando ST_GeometryN(..., 1)."""
    insp = _inspect_point_geometry(schema, table)
    if insp.get("status") == "already_point":
        insp["converted"] = False
        insp["conversion_applied"] = False
        return insp
    if not insp.get("can_convert_to_point"):
        raise ValueError(insp.get("message") or "CONVERT_NOT_SAFE")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '180s'")
            cur.execute(
                f"""
                ALTER TABLE "{schema}"."{table}"
                  ALTER COLUMN the_geom
                  TYPE geometry(Point, 3857)
                  USING CASE
                    WHEN the_geom IS NULL THEN NULL
                    ELSE ST_SetSRID(
                      ST_GeometryN(the_geom, 1),
                      COALESCE(NULLIF(ST_SRID(the_geom), 0), 3857)
                    )
                  END
                """
            )
        conn.commit()

    after = _inspect_point_geometry(schema, table)
    after.update(
        {
            "original_type": "MULTIPOINT",
            "elements_per_geometry": 1,
            "converted": True,
            "conversion_applied": True,
            "can_convert_to_point": False,
            "status": "converted",
            "final_type": "POINT",
            "message": (
                "Geometría optimizada a POINT "
                "(todos los registros contenían un único punto)."
            ),
        }
    )
    return after


def normalize_job_geometry(job_id: str, *, accept: bool = True) -> Dict[str, Any]:
    """Aplica o declina la normalización MULTIPOINT→POINT en staging."""
    jid = assert_job_id(job_id)
    job = get_job(jid)
    if not job:
        raise ValueError("JOB_NOT_FOUND")
    if job["status"] != "ready":
        raise ValueError(f"JOB_NOT_READY:{job['status']}")

    stg = (job.get("staging_table") or staging_table_name(jid)).strip()
    if not _table_exists(STAGING_SCHEMA, stg):
        raise ValueError("STAGING_MISSING")

    report = dict(job.get("report") or {})
    if not accept:
        gv = dict(report.get("geometry_validation") or {})
        gv["user_declined"] = True
        gv["conversion_applied"] = False
        gv["converted"] = False
        if gv.get("can_convert_to_point"):
            gv["message"] = (
                "Conversión a POINT declinada por el administrador. "
                "Se mantendrá MULTIPOINT en el swap."
            )
        report["geometry_validation"] = gv
        infos = list(report.get("infos") or [])
        note = "Normalización POINT declinada; staging permanece MULTIPOINT."
        if note not in infos:
            infos.append(note)
        report["infos"] = infos
        update_job(jid, report=report)
        return get_job(jid) or job

    gv = _convert_table_to_point(STAGING_SCHEMA, stg)
    target = assert_table_name(job["target_table"])
    report["geometry_validation"] = gv
    report["geometry_staging"] = "point"
    report["geometry_staging_detail"] = "point"
    report["geometry_production"] = _geometry_type(SCHEMA, target)
    report["geometry_production_detail"] = _geometry_type_detail(SCHEMA, target)

    warnings = [
        w
        for w in (report.get("warnings") or [])
        if "geometría distinto" not in str(w).lower()
        and "multipoint" not in str(w).lower()
    ]
    infos = list(report.get("infos") or [])
    msg = str(gv.get("message") or "Geometría optimizada a POINT.")
    infos = [i for i in infos if "MULTIPOINT" not in i and "convertir a POINT" not in i]
    infos.insert(0, msg)
    report["warnings"] = warnings
    report["infos"] = infos

    geom_ok = report["geometry_production"] == _geometry_type(STAGING_SCHEMA, stg)
    validation = dict(report.get("validation") or {})
    checks = dict(validation.get("checks") or {})
    checks["geometry"] = geom_ok
    validation["checks"] = checks
    if geom_ok and checks.get("columns", True):
        validation["level"] = "ok"
        validation["label"] = "Compatible"
    report["validation"] = validation

    update_job(jid, report=report, error_message="")
    return get_job(jid) or job


def _diff_report(target: str, staging: str) -> Dict[str, Any]:
    """Resumen rápido: columnas + geometría + conteos (con timeout). Sin locks largos."""
    prod_info = _safe_count(SCHEMA, target)
    stg_info = _safe_count(STAGING_SCHEMA, staging)
    prod_count = int(prod_info["count"])
    stg_count = int(stg_info["count"])
    # Si el estimado de staging quedó en 0 tras ogr2ogr, un COUNT corto de respaldo
    if stg_count <= 0:
        stg_info = _safe_count(STAGING_SCHEMA, staging, timeout_s=30)
        stg_count = int(stg_info["count"])

    prod_cols = _column_names(SCHEMA, target)
    stg_cols = _column_names(STAGING_SCHEMA, staging)
    only_prod = sorted(prod_cols - stg_cols)
    only_stg = sorted(stg_cols - prod_cols)
    common = sorted(prod_cols & stg_cols)
    key = _pick_key(prod_cols, stg_cols)

    # Capas grandes o conteo inexacto → no intentar EXCEPT
    largest = max(prod_count, stg_count)
    if key and largest > _KEY_DIFF_MAX_FEATURES:
        key_stats = {
            "key": key,
            "skipped": True,
            "reason": "large_table",
            "note": (
                f"Diff detallado omitido ({largest:,} feats > {_KEY_DIFF_MAX_FEATURES:,}). "
                "El swap reemplaza la tabla completa."
            ),
        }
    elif key:
        key_stats = _key_diff_stats(target, staging, key, prod_count, stg_count)
    else:
        key_stats = {
            "key": None,
            "note": (
                "Sin clave común (gid/cve_mun/cvegeo/id); solo se comparan conteos y columnas. "
                "El apply hace swap completo de tabla."
            ),
        }

    prod_geom = _geometry_type(SCHEMA, target)
    stg_geom = _geometry_type(STAGING_SCHEMA, staging)
    prod_detail = _geometry_type_detail(SCHEMA, target)
    stg_detail = _geometry_type_detail(STAGING_SCHEMA, staging)
    geom_validation = _inspect_point_geometry(STAGING_SCHEMA, staging)

    warnings: List[str] = []
    infos: List[str] = []
    if only_prod:
        warnings.append(
            f"Columnas solo en producción (se perderán en swap): {', '.join(only_prod)}"
        )
    if only_stg:
        warnings.append(f"Columnas nuevas en staging: {', '.join(only_stg)}")

    geom_family_ok = prod_geom == stg_geom
    if not geom_family_ok:
        warnings.append(
            f"Tipo de geometría distinto: prod={prod_detail} staging={stg_detail}"
        )
    elif prod_detail != stg_detail:
        if geom_validation.get("can_convert_to_point"):
            infos.append(
                "Staging es MULTIPOINT con un solo punto por registro. "
                "Se recomienda convertir a POINT antes del swap."
            )
        elif geom_validation.get("status") == "unsafe":
            warnings.append(
                str(geom_validation.get("message") or "MULTIPOINT complejo.")
            )
        else:
            infos.append(
                f"Detalle de geometría: producción={prod_detail}, staging={stg_detail}."
            )

    if key_stats.get("skipped") and key_stats.get("reason") == "large_table":
        infos.append(
            f"Optimización aplicada: debido al tamaño de la tabla ({largest:,} registros), "
            "no se calculó el diff fila por fila. El reemplazo se realizará mediante intercambio "
            "completo de tablas (Swap), lo que garantiza un proceso más rápido y seguro."
        )
    elif key_stats.get("skipped"):
        infos.append(
            str(key_stats.get("note") or "Diff por clave omitido (optimización / timeout).")
        )
    if not prod_info.get("exact") or not stg_info.get("exact"):
        infos.append(
            "Conteos parcialmente estimados (pg_class) para evitar bloqueos en tablas grandes."
        )

    # SRID staging vs producción
    prod_srid = _table_srid(SCHEMA, target)
    stg_srid = _table_srid(STAGING_SCHEMA, staging)
    srid_ok = True
    if prod_srid is not None and stg_srid is not None and int(prod_srid) != int(stg_srid):
        srid_ok = False
        warnings.append(
            f"SRID distinto: producción={prod_srid}, staging={stg_srid}."
        )

    # Duplicados de clave en staging
    dup_count = 0
    if key and not key_stats.get("skipped"):
        dup_count = _staging_duplicate_keys(STAGING_SCHEMA, staging, key)
        if dup_count > 0:
            warnings.append(
                f"{dup_count} claves duplicadas en staging ({key}); el swap igual las conserva."
            )

    # Nulos en columnas críticas (warn)
    null_warns = _critical_null_warnings(STAGING_SCHEMA, staging, key)
    warnings.extend(null_warns)

    # GIST en producción (info/warn)
    gist_ok = _has_gist_index(SCHEMA, target)
    if not gist_ok:
        infos.append(
            "Producción no tiene índice GIST detectado; se intentará recrear al aplicar."
        )

    cols_ok = not only_prod
    convertible = bool(geom_validation.get("can_convert_to_point"))
    unsafe_mp = geom_validation.get("status") == "unsafe"
    counts_ok = stg_count > 0
    if not geom_family_ok or unsafe_mp:
        level = "block"
        label = "Geometría incompatible"
        geom_check = False
    elif not srid_ok:
        level = "block"
        label = "SRID incompatible"
        geom_check = True
    elif not counts_ok:
        level = "block"
        label = "Staging vacío"
        geom_check = True
    elif convertible and stg_detail == "multipoint":
        level = "warn"
        label = "Optimización recomendada"
        geom_check = True
    elif dup_count > 0 or null_warns or (geom_family_ok and not cols_ok):
        level = "warn"
        label = "Revisar advertencias"
        geom_check = True
    elif geom_family_ok and cols_ok:
        level = "ok"
        label = "Compatible"
        geom_check = True
    else:
        level = "block"
        label = "Geometría incompatible"
        geom_check = False

    checklist = [
        {
            "id": "columns",
            "label": "Columnas / estructura",
            "status": "ok" if cols_ok else "warn",
            "detail": (
                "OK"
                if cols_ok
                else f"Solo en prod: {', '.join(only_prod[:6])}"
            ),
        },
        {
            "id": "geometry",
            "label": "Geometría / familia",
            "status": (
                "block"
                if not geom_family_ok or unsafe_mp
                else ("warn" if convertible and stg_detail == "multipoint" else "ok")
            ),
            "detail": f"prod={prod_detail} staging={stg_detail}",
        },
        {
            "id": "srid",
            "label": "SRID consistente",
            "status": "ok" if srid_ok else "block",
            "detail": f"prod={prod_srid} staging={stg_srid}",
        },
        {
            "id": "counts",
            "label": "Conteo / delta",
            "status": "ok" if counts_ok else "block",
            "detail": f"Δ={stg_count - prod_count:+d}",
        },
        {
            "id": "duplicates",
            "label": "Duplicados de clave",
            "status": "warn" if dup_count > 0 else ("info" if not key else "ok"),
            "detail": (
                f"{dup_count} dups" if dup_count else (f"clave={key}" if key else "sin clave")
            ),
        },
        {
            "id": "nulls",
            "label": "Nulos en columnas críticas",
            "status": "warn" if null_warns else "ok",
            "detail": null_warns[0] if null_warns else "OK",
        },
        {
            "id": "gist",
            "label": "Índice GIST (prod)",
            "status": "ok" if gist_ok else "warn",
            "detail": "presente" if gist_ok else "ausente / se recreará",
        },
    ]

    validation = {
        "level": level,
        "label": label,
        "checks": {
            "geometry": geom_check and (geom_family_ok and not unsafe_mp),
            "structure": True,
            "columns": cols_ok,
            "counts": counts_ok,
            "srid": srid_ok,
            "duplicates": dup_count == 0,
            "swap_available": level != "block",
        },
        "checklist": checklist,
    }

    return {
        "target_table": target,
        "staging_table": staging,
        "production_count": prod_count,
        "staging_count": stg_count,
        "delta_count": stg_count - prod_count,
        "counts_exact": bool(prod_info.get("exact") and stg_info.get("exact")),
        "columns_common": common,
        "columns_only_production": only_prod,
        "columns_only_staging": only_stg,
        "columns_common_count": len(common),
        "columns_expected_count": len(prod_cols) if prod_cols else len(common),
        "geometry_production": prod_geom,
        "geometry_staging": stg_geom,
        "geometry_production_detail": prod_detail,
        "geometry_staging_detail": stg_detail,
        "geometry_validation": geom_validation,
        "srid_production": prod_srid,
        "srid_staging": stg_srid,
        "duplicate_keys": dup_count,
        "key_diff": key_stats,
        "warnings": warnings,
        "infos": infos,
        "validation": validation,
        "strategy": "full_table_swap_rename",
        "strategy_label": "Reemplazo completo (Swap)",
    }


def _phase_report(
    *,
    filename: str,
    phase: str,
    progress: int,
    label: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "filename": filename,
        "phase": phase,
        "progress": max(0, min(100, int(progress))),
        "label": label,
    }
    if extra:
        out.update(extra)
    return out


def enqueue_job_from_upload(
    *,
    content: bytes,
    filename: str,
    target_table: str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Acepta el archivo, crea el job y deja el trabajo pesado para ``run_job_pipeline``."""
    target = assert_table_name(target_table)
    if not _table_exists(SCHEMA, target):
        raise ValueError(f"TARGET_NOT_FOUND:{target}")
    if not content:
        raise ValueError("EMPTY_FILE")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in (".shp", ".zip"):
        raise ValueError("UNSUPPORTED_FORMAT")

    ensure_staging_schema()
    job_id = new_job_id()
    stg = staging_table_name(job_id)
    work = Path(tempfile.mkdtemp(prefix=f"data_refresh_{job_id}_"))
    archive = work / f"upload{suffix}"
    archive.write_bytes(content)
    _JOB_WORKDIRS[job_id] = work

    insert_job(
        job_id=job_id,
        user_id=user_id,
        target_table=target,
        staging_table=stg,
        status="queued",
        report=_phase_report(
            filename=filename or archive.name,
            phase="queued",
            progress=15,
            label="En cola: importación pendiente",
        ),
    )
    return get_job(job_id) or {
        "id": job_id,
        "status": "queued",
        "target_table": target,
        "staging_table": stg,
        "report": {},
    }


def run_job_pipeline(job_id: str) -> None:
    """Import ogr2ogr + diff (se ejecuta en background tras el POST)."""
    job = get_job(job_id)
    if not job:
        return
    if job.get("status") == "cancelled":
        _cleanup_workdir(job_id)
        return

    target = assert_table_name(job["target_table"])
    stg = (job.get("staging_table") or staging_table_name(job_id)).strip()
    filename = str((job.get("report") or {}).get("filename") or "upload")
    work = _JOB_WORKDIRS.get(job_id)

    try:
        if not work or not work.exists():
            cur = get_job(job_id)
            if cur and cur.get("status") == "cancelled":
                return
            raise RuntimeError("WORKDIR_MISSING")

        archives = list(work.glob("upload.*"))
        if not archives:
            raise RuntimeError("UPLOAD_FILE_MISSING")
        archive = archives[0]

        update_job(
            job_id,
            status="importing",
            report=_phase_report(
                filename=filename,
                phase="importing",
                progress=35,
                label="Importando SHP a staging (ogr2ogr)…",
            ),
        )
        _drop_table(STAGING_SCHEMA, stg)
        folder = _extract_shp_dir(archive)
        shp_path = _find_shp_file(folder)
        # Capas punto: importar como POINT (evita MULTIPOINT por PROMOTE_TO_MULTI).
        nlt = "POINT" if _geometry_type(SCHEMA, target) == "point" else "PROMOTE_TO_MULTI"
        _ogr2ogr_import(shp_path, stg, STAGING_SCHEMA, nlt=nlt)

        if not _table_exists(STAGING_SCHEMA, stg):
            raise RuntimeError("STAGING_IMPORT_FAILED")

        # Stats rápidas para que comparing use estimado fiable (timeout duro).
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '45s'")
                    cur.execute(f'ANALYZE "{STAGING_SCHEMA}"."{stg}"')
                conn.commit()
        except Exception:
            logger.warning("ANALYZE staging omitido/timeout job=%s", job_id)

        cur_job = get_job(job_id)
        if cur_job and cur_job.get("status") == "cancelled":
            _drop_table(STAGING_SCHEMA, stg)
            return

        update_job(
            job_id,
            status="comparing",
            report=_phase_report(
                filename=filename,
                phase="comparing",
                progress=75,
                label="Comparando staging vs producción (resumen)…",
                extra={"target_table": target, "staging_table": stg},
            ),
        )
        report = _diff_report(target, stg)
        report["filename"] = filename
        report["phase"] = "ready"
        report["progress"] = 100
        report["label"] = "Comparación lista"

        cur_job = get_job(job_id)
        if cur_job and cur_job.get("status") == "cancelled":
            _drop_table(STAGING_SCHEMA, stg)
            return

        update_job(job_id, status="ready", report=report, error_message="")
    except Exception as exc:
        logger.exception("data_refresh pipeline failed job=%s", job_id)
        update_job(
            job_id,
            status="failed",
            error_message=str(exc)[:800],
            report=_phase_report(
                filename=filename,
                phase="failed",
                progress=100,
                label="Error en importación/comparación",
                extra={"error": str(exc)[:800]},
            ),
        )
        try:
            _drop_table(STAGING_SCHEMA, stg)
        except Exception:
            pass
    finally:
        _cleanup_workdir(job_id)


def _cleanup_workdir(job_id: str) -> None:
    work = _JOB_WORKDIRS.pop(job_id, None)
    if work is not None:
        shutil.rmtree(work, ignore_errors=True)


# Compat: nombre anterior (tests / imports externos).
def create_job_from_upload(
    *,
    content: bytes,
    filename: str,
    target_table: str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    job = enqueue_job_from_upload(
        content=content,
        filename=filename,
        target_table=target_table,
        user_id=user_id,
    )
    run_job_pipeline(str(job["id"]))
    out = get_job(str(job["id"]))
    if not out:
        raise RuntimeError("JOB_LOST")
    if out.get("status") == "failed":
        raise RuntimeError(out.get("error_message") or "JOB_FAILED")
    return out


def apply_job(job_id: str, *, user_id: Optional[int] = None) -> Dict[str, Any]:
    import time

    from data_refresh.versions import register_version, version_backup_name

    jid = assert_job_id(job_id)
    job = get_job(jid)
    if not job:
        raise ValueError("JOB_NOT_FOUND")
    if job["status"] != "ready":
        raise ValueError(f"JOB_NOT_READY:{job['status']}")

    report0 = job.get("report") or {}
    if (report0.get("validation") or {}).get("level") == "block":
        raise ValueError("JOB_BLOCKED_VALIDATION")

    target = assert_table_name(job["target_table"])
    stg = (job.get("staging_table") or staging_table_name(jid)).strip()
    if not _table_exists(STAGING_SCHEMA, stg):
        raise ValueError(
            "STAGING_MISSING: la tabla temporal ya no existe. "
            "Vuelva a subir y comparar antes de aplicar."
        )
    if not _table_exists(SCHEMA, target):
        raise ValueError(f"TARGET_NOT_FOUND:{target}")

    bak = f"{target}__dr_bak_{jid}"[:63]
    ver_name = version_backup_name(target, jid)
    update_job(jid, status="applying")
    t0 = time.monotonic()
    indexes_ok = False
    indexes_note = "Sin recrear"

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '45s'")
                cur.execute("SET LOCAL statement_timeout = '180s'")
                cur.execute(f'DROP TABLE IF EXISTS "{SCHEMA}"."{bak}" CASCADE')
                cur.execute(f'ALTER TABLE "{SCHEMA}"."{target}" RENAME TO "{bak}"')
                cur.execute(
                    f'ALTER TABLE "{STAGING_SCHEMA}"."{stg}" SET SCHEMA "{SCHEMA}"'
                )
                cur.execute(f'ALTER TABLE "{SCHEMA}"."{stg}" RENAME TO "{target}"')
                indexes_ok, indexes_note = _ensure_spatial_index(cur, SCHEMA, target)
                try:
                    _clone_useful_indexes(cur, SCHEMA, bak, target)
                    indexes_ok = True
                    indexes_note = "Índice espacial y clones útiles desde backup"
                except Exception as idx_exc:
                    indexes_note = f"Índice espacial OK; clones parciales: {str(idx_exc)[:120]}"
                cur.execute(f'ANALYZE "{SCHEMA}"."{target}"')
                # Retener versión (no DROP): renombrar bak → nombre estable
                cur.execute(f'DROP TABLE IF EXISTS "{SCHEMA}"."{ver_name}" CASCADE')
                cur.execute(f'ALTER TABLE "{SCHEMA}"."{bak}" RENAME TO "{ver_name}"')
            conn.commit()

        try:
            register_version(
                table_name=target,
                backup_table=ver_name,
                job_id=jid,
                kind="spatial",
            )
        except Exception as ver_exc:
            logger.warning("No se registró versión %s: %s", ver_name, ver_exc)

        final_count = _feature_count(SCHEMA, target)
        elapsed = round(time.monotonic() - t0, 1)
        martin = _wait_martin_soft(target, timeout_s=20)
        martin_ok = bool(martin.get("ok"))
        elapsed = round(time.monotonic() - t0, 1)

        report = dict(job.get("report") or {})
        report["applied"] = True
        report["final_count"] = final_count
        report["martin"] = martin
        report["apply_summary"] = {
            "table_replaced": True,
            "backup_table": ver_name,
            "indexes_ok": indexes_ok,
            "indexes_note": indexes_note,
            "analyzed": True,
            "martin_detected": martin_ok,
            "published": martin_ok,
            "elapsed_seconds": elapsed,
            "checks": [
                {"ok": True, "label": "Tabla reemplazada"},
                {"ok": True, "label": f"Versión retenida: {ver_name}"},
                {
                    "ok": indexes_ok,
                    "label": "Índices" + (" preservados/recreados" if indexes_ok else " (revisar)"),
                },
                {"ok": True, "label": "Estadísticas actualizadas (ANALYZE)"},
                {
                    "ok": martin_ok,
                    "label": (
                        "Martin detectó la nueva capa"
                        if martin_ok
                        else "Martin aún no confirma la capa (puede tardar unos segundos)"
                    ),
                },
                {
                    "ok": martin_ok,
                    "label": (
                        "Publicación disponible"
                        if martin_ok
                        else "Publicación pendiente de Martin"
                    ),
                },
            ],
        }
        report["phase"] = "applied"
        report["progress"] = 100
        report["label"] = "Swap aplicado"
        update_job(jid, status="applied", report=report, error_message="")

        if user_id is not None:
            try:
                record_audit(
                    int(user_id),
                    "data_refresh_apply",
                    target,
                    {"job_id": jid, "before": job.get("report")},
                    {"job_id": jid, "after": report},
                )
            except Exception:
                pass

        return get_job(jid) or job
    except Exception as exc:
        # Intentar rollback si el bak sigue existiendo y target no
        staging_still = False
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    if not _table_exists(SCHEMA, target):
                        if _table_exists(SCHEMA, bak):
                            cur.execute(
                                f'ALTER TABLE "{SCHEMA}"."{bak}" RENAME TO "{target}"'
                            )
                        elif _table_exists(SCHEMA, ver_name):
                            cur.execute(
                                f'ALTER TABLE "{SCHEMA}"."{ver_name}" RENAME TO "{target}"'
                            )
                    # Si staging quedó a medias en atlas como dr_*, intentar devolverla
                    if _table_exists(SCHEMA, stg) and _table_exists(SCHEMA, target):
                        cur.execute(
                            f'ALTER TABLE "{SCHEMA}"."{stg}" SET SCHEMA "{STAGING_SCHEMA}"'
                        )
                        staging_still = True
                    elif _table_exists(STAGING_SCHEMA, stg):
                        staging_still = True
                conn.commit()
        except Exception:
            pass
        # Si staging sigue, dejar ready para reintentar; si no, failed
        retry_status = "ready" if staging_still else "failed"
        update_job(
            jid,
            status=retry_status,
            error_message=str(exc)[:800],
            report={
                **(job.get("report") or {}),
                "apply_error": str(exc)[:800],
            },
        )
        raise


def _ensure_spatial_index(cur, schema: str, table: str) -> Tuple[bool, str]:
    idx = f"{table}_the_geom_gix"[:63]
    try:
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS "{idx}"
              ON "{schema}"."{table}"
              USING GIST ("the_geom")
            """
        )
        return True, f"GIST {idx}"
    except Exception as exc:
        return False, str(exc)[:160]


def _clone_useful_indexes(cur, schema: str, source_table: str, dest_table: str) -> None:
    """Recrea índices no-PK del backup sobre la tabla nueva (mejores nombres)."""
    cur.execute(
        """
        SELECT indexname, indexdef
          FROM pg_indexes
         WHERE schemaname = %s AND tablename = %s
        """,
        (schema, source_table),
    )
    rows = cur.fetchall() or []
    for row in rows:
        name = str(row.get("indexname") or "")
        defn = str(row.get("indexdef") or "")
        if not defn or " UNIQUE " in defn.upper() and "CONSTRAINT" in defn.upper():
            continue
        if name.endswith("_pkey") or name.endswith("_pkey1"):
            continue
        # Sustituir nombre de tabla backup → destino
        new_def = defn.replace(f'"{source_table}"', f'"{dest_table}"')
        new_def = new_def.replace(f" {source_table} ", f" {dest_table} ")
        # Evitar chocar con índice ya creado
        new_name = name.replace(source_table, dest_table)
        if new_name == name:
            new_name = f"{dest_table}_{name}"[-63:]
        new_def = new_def.replace(f'INDEX "{name}"', f'INDEX IF NOT EXISTS "{new_name}"', 1)
        new_def = new_def.replace(f"INDEX {name} ", f'INDEX IF NOT EXISTS "{new_name}" ', 1)
        try:
            cur.execute(new_def)
        except Exception:
            continue


def cancel_job(job_id: str) -> Dict[str, Any]:
    jid = assert_job_id(job_id)
    job = get_job(jid)
    if not job:
        raise ValueError("JOB_NOT_FOUND")
    if job["status"] == "applied":
        raise ValueError("JOB_ALREADY_APPLIED")
    if job["status"] == "applying":
        raise ValueError("JOB_BUSY:applying")
    stg = job.get("staging_table")
    if stg:
        try:
            _drop_table(STAGING_SCHEMA, stg)
        except Exception:
            pass
    _cleanup_workdir(jid)
    update_job(jid, status="cancelled")
    return get_job(jid) or job


def _wait_martin_soft(table: str, timeout_s: int = 20) -> Dict[str, Any]:
    try:
        from visor_martin_ready import wait_for_martin_table

        return wait_for_martin_table(table, timeout_s=timeout_s)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
