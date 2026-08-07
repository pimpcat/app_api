"""Servicios públicos: health, texto dinámico por pestaña."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from database import get_db
from geography_context import ENGINE_NAME, __version__
from geography_context.catalog_loader import (
    assert_sql_ident,
    catalog_status,
    enabled_tabs,
    load_geography_catalog_raw,
)
from tables import SCHEMA

_bulk_cache: Dict[str, Any] = {"key": None, "rows": None}


def invalidate_texto_cache() -> None:
    _bulk_cache["key"] = None
    _bulk_cache["rows"] = None


def health_payload() -> Dict[str, Any]:
    cat = catalog_status()
    db_ok = False
    db_detail: Dict[str, Any] = {"ok": False}
    try:
        catalog = load_geography_catalog_raw()
        defaults = catalog.get("defaults") or {}
        tabs = enabled_tabs(catalog)
        sample_table = None
        if tabs:
            sample_table = (tabs[0].get("text") or {}).get("table")
        if sample_table:
            assert_sql_ident(sample_table, label="table")
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT 1
                          FROM information_schema.tables
                         WHERE table_schema = %s AND table_name = %s
                         LIMIT 1
                        """,
                        (SCHEMA, sample_table),
                    )
                    db_ok = cur.fetchone() is not None
            db_detail = {
                "ok": db_ok,
                "schema": SCHEMA,
                "sample_table": sample_table,
                "ent_value": defaults.get("ent_value"),
            }
        else:
            db_detail = {"ok": False, "error": "NO_TABS"}
    except Exception as exc:
        db_detail = {"ok": False, "error": str(exc)}

    return {
        "engine": ENGINE_NAME,
        "version": __version__,
        "enabled": True,
        "capabilities": [
            "tabs",
            "macro_map",
            "detail_map",
            "visor_layers",
            "dynamic_text",
            "legend",
        ],
        "catalog": cat,
        "contexto_db": db_detail,
    }


def _group_tabs(
    tabs: List[Dict[str, Any]], defaults: Dict[str, Any]
) -> List[Tuple[str, str, str, str, List[Tuple[str, str]]]]:
    """Agrupa pestañas por (table, key_column, ent_column, ent_value) → [(tab_id, field)]."""
    groups: Dict[Tuple[str, str, str, str], List[Tuple[str, str]]] = {}
    ent_col = assert_sql_ident(
        str(defaults.get("ent_column") or "ent"), label="ent_column"
    )
    ent_val = str(defaults.get("ent_value") or "12")
    for tab in tabs:
        text = tab.get("text") or {}
        table = assert_sql_ident(str(text.get("table") or ""), label="table")
        field = assert_sql_ident(str(text.get("field") or ""), label="field")
        key = assert_sql_ident(
            str(text.get("key_column") or defaults.get("key_column") or "cve_mun"),
            label="key_column",
        )
        gkey = (table, key, ent_col, ent_val)
        groups.setdefault(gkey, []).append((tab["id"], field))
    return [(t, k, e, v, fields) for (t, k, e, v), fields in groups.items()]


def _norm_cve(cve: Any) -> str:
    digits = "".join(ch for ch in str(cve or "") if ch.isdigit())
    if not digits:
        return ""
    return digits[-3:].zfill(3) if len(digits) >= 3 else digits.zfill(3)


def _load_bulk_rows() -> Dict[str, Dict[str, Any]]:
    catalog = load_geography_catalog_raw()
    defaults = catalog.get("defaults") or {}
    tabs = enabled_tabs(catalog)
    cache_key = str(
        [
            (
                t["id"],
                (t.get("text") or {}).get("table"),
                (t.get("text") or {}).get("field"),
                (t.get("text") or {}).get("key_column"),
            )
            for t in tabs
        ]
        + [defaults.get("ent_column"), defaults.get("ent_value")]
    )
    if _bulk_cache["key"] == cache_key and _bulk_cache["rows"] is not None:
        return _bulk_cache["rows"]

    rows: Dict[str, Dict[str, Any]] = {}
    for table, key_col, ent_col, ent_val, fields in _group_tabs(tabs, defaults):
        field_names = sorted({f for _, f in fields})
        select_cols = ", ".join(
            [f"TRIM({key_col}::text) AS _key"]
            + [f"{f} AS {f}" for f in field_names]
        )
        sql = f"""
          SELECT {select_cols}
            FROM {SCHEMA}.{table}
           WHERE TRIM({ent_col}::text) = %s
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (ent_val,))
                for db_row in cur.fetchall():
                    cve = _norm_cve(db_row.get("_key"))
                    if not cve:
                        continue
                    out = rows.setdefault(cve, {})
                    for tab_id, field in fields:
                        out[tab_id] = db_row.get(field)

    _bulk_cache["key"] = cache_key
    _bulk_cache["rows"] = rows
    return rows


def get_contexto_row(cve_mun: str) -> Optional[Dict[str, Any]]:
    cve = _norm_cve(cve_mun)
    if not cve:
        return None
    return _load_bulk_rows().get(cve)


def get_contexto_all() -> Dict[str, Dict[str, Any]]:
    return _load_bulk_rows()


def public_catalog_payload() -> Dict[str, Any]:
    cat = load_geography_catalog_raw()
    tabs = enabled_tabs(cat)
    return {
        "ok": True,
        "engine": ENGINE_NAME,
        "version": __version__,
        "catalog": {
            "version": cat.get("version"),
            "menu": cat.get("menu"),
            "layout": cat.get("layout"),
            "defaults": cat.get("defaults"),
            "tabs": tabs,
        },
    }
