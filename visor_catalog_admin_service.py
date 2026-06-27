"""Servicios del asistente admin: tablas Martin, columnas PostGIS, alta de capas."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from config import get_settings
from database import get_db
from visor_catalog_loader import load_visor_catalog_raw
from visor_catalog_validate import (
    build_layer_entry,
    load_icons_meta,
    load_preset_meta,
    slug_layer_id,
    validate_layer_payload,
    validate_layer_update_payload,
)
from visor_catalog_writer import (
    catalog_layer_ids,
    catalog_table_names,
    delete_layer_entry,
    find_layer_group_id,
    load_catalog_mutable,
    merge_layer_entry,
    replace_layer_entry,
    save_catalog,
)

from auth.users import ADMIN_SCHEMA

MARTIN_CATALOG_URL = os.getenv("MARTIN_CATALOG_URL", "http://martin:3000/catalog").strip()


def _martin_catalog_layer_ids(catalog: Any) -> List[str]:
    ids: List[str] = []
    if isinstance(catalog, dict):
        tiles = catalog.get("tiles")
        if isinstance(tiles, dict):
            ids.extend(str(k) for k in tiles.keys())
        else:
            ids.extend(str(k) for k in catalog.keys() if k not in ("tiles", "sprites", "fonts"))
    elif isinstance(catalog, list):
        for item in catalog:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
    return ids


def fetch_martin_table_ids() -> List[str]:
    try:
        req = urllib.request.Request(
            MARTIN_CATALOG_URL,
            headers={"User-Agent": "AtlasGro/visor-admin"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            catalog = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise RuntimeError(f"MARTIN_UNAVAILABLE:{exc}") from exc
    return sorted(
        {
            lid
            for lid in _martin_catalog_layer_ids(catalog)
            if lid.startswith("c_") or lid.startswith("v_c_")
        }
    )


def list_publishable_tables() -> List[Dict[str, str]]:
    martin_ids = fetch_martin_table_ids()
    used = catalog_table_names()
    out: List[Dict[str, str]] = []
    for table in martin_ids:
        if table.lower() in used:
            continue
        out.append({"table": table, "label": table})
    return out


def list_table_columns(table: str) -> List[Dict[str, str]]:
    name = (table or "").strip()
    if not name or not name.replace("_", "").isalnum():
        raise ValueError("INVALID_TABLE")
    settings = get_settings()
    schema = settings.get("schema") or "atlas"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type, udt_name
                  FROM information_schema.columns
                 WHERE table_schema = %s AND table_name = %s
                 ORDER BY ordinal_position
                """,
                (schema, name),
            )
            rows = cur.fetchall()
    if not rows:
        raise ValueError("TABLE_NOT_FOUND")
    skip = {"the_geom", "geom", "wkb_geometry"}
    return [
        {
            "name": r["column_name"],
            "type": r["data_type"],
            "udt": r["udt_name"],
        }
        for r in rows
        if (r["column_name"] or "").lower() not in skip
        and r["udt_name"] not in ("geometry", "geography")
    ]


def list_column_distinct_values(table: str, column: str, limit: int = 32) -> Dict[str, Any]:
    """Valores distintos de una columna (para autoclasificar simbología por atributo)."""
    name = (table or "").strip()
    col = (column or "").strip()
    if not name or not name.replace("_", "").isalnum():
        raise ValueError("INVALID_TABLE")
    if not col or not col.replace("_", "").isalnum():
        raise ValueError("INVALID_COLUMN")
    lim = max(1, min(int(limit or 32), 64))

    columns = list_table_columns(name)
    match = next((c for c in columns if (c.get("name") or "").lower() == col.lower()), None)
    if not match:
        raise ValueError("COLUMN_NOT_FOUND")

    actual_col = match["name"]
    settings = get_settings()
    schema = settings.get("schema") or "atlas"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT COUNT(DISTINCT "{actual_col}") AS n FROM "{schema}"."{name}" '
                f'WHERE "{actual_col}" IS NOT NULL'
            )
            total_row = cur.fetchone()
            total = int(total_row["n"]) if total_row and total_row.get("n") is not None else 0
            cur.execute(
                f'SELECT DISTINCT "{actual_col}"::text AS val FROM "{schema}"."{name}" '
                f'WHERE "{actual_col}" IS NOT NULL ORDER BY 1 LIMIT %s',
                (lim,),
            )
            rows = cur.fetchall()

    values: List[str] = []
    for r in rows:
        raw = r.get("val")
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            values.append(text)

    return {
        "table": name,
        "column": actual_col,
        "values": values,
        "total_distinct": total,
        "truncated": total > len(values),
        "limit": lim,
    }


def admin_meta() -> Dict[str, Any]:
    raw = load_visor_catalog_raw()
    groups = raw.get("groups") or []
    group_list = [
        {"id": g.get("id"), "label": g.get("label") or g.get("id")}
        for g in groups
        if isinstance(g, dict) and g.get("id")
    ]
    return {
        "presets": load_preset_meta(),
        "icons": load_icons_meta(),
        "groups": group_list,
        "phase": 3,
    }


def table_publish_status(table: str) -> Dict[str, Any]:
    name = (table or "").strip()
    if not name:
        raise ValueError("INVALID_TABLE")
    try:
        martin_ids = {t.lower() for t in fetch_martin_table_ids()}
    except RuntimeError:
        martin_ids = set()
    in_martin = name.lower() in martin_ids
    in_catalog = name.lower() in catalog_table_names()
    return {
        "table": name,
        "in_martin": in_martin,
        "in_catalog": in_catalog,
        "needs_martin_restart": not in_martin,
        "is_denue_table": name.lower() == "c_denue",
    }


def _managed_layer_ids() -> set[str]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT layer_id FROM {ADMIN_SCHEMA}.layer_publications")
            rows = cur.fetchall()
    return {str(r["layer_id"]).strip().lower() for r in rows if r.get("layer_id")}


def list_managed_layers() -> List[Dict[str, Any]]:
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    managed = _managed_layer_ids()
    out: List[Dict[str, Any]] = []
    if not isinstance(layers, dict):
        return out
    for layer_id, entry in layers.items():
        if not isinstance(entry, dict):
            continue
        if layer_id.lower() not in managed:
            continue
        data = entry.get("data") or {}
        out.append(
            {
                "layer_id": layer_id,
                "label": entry.get("label") or layer_id,
                "table": data.get("table") or "",
                "group_id": find_layer_group_id(raw, layer_id) or "",
                "geometry": entry.get("geometry") or "",
                "style_preset": entry.get("style_preset") or "",
                "mun_filter": data.get("mun_filter"),
            }
        )
    out.sort(key=lambda x: (x.get("label") or "").lower())
    return out


def _normalize_identify_fields(fields: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in fields or ["gid"]:
        if isinstance(item, str) and item.strip():
            col = item.strip()
            out.append({"column": col, "label": col})
        elif isinstance(item, dict):
            col = item.get("column") or item.get("field") or item.get("name")
            if col and str(col).strip():
                col_s = str(col).strip()
                label = item.get("label")
                out.append(
                    {
                        "column": col_s,
                        "label": str(label).strip() if label else col_s,
                    }
                )
    return out or [{"column": "gid", "label": "gid"}]


def _normalize_labels_for_admin(entry: Dict[str, Any]) -> Dict[str, Any]:
    labels = entry.get("labels") or {}
    geometry = entry.get("geometry") or "point"
    default_minz = 16 if geometry == "line" else 14
    if not labels or labels.get("enabled") is False or not labels.get("field"):
        return {
            "enabled": False,
            "field": "",
            "minzoom": default_minz,
            "above_icon": True,
            "color": "#2c3e50",
        }
    return {
        "enabled": True,
        "field": str(labels.get("field") or "").strip(),
        "minzoom": labels.get("minzoom", default_minz),
        "above_icon": labels.get("above_icon", True),
        "color": labels.get("color") or "#2c3e50",
    }


def get_layer_admin_detail(layer_id: str) -> Dict[str, Any]:
    lid = slug_layer_id(layer_id)
    if lid not in _managed_layer_ids():
        raise ValueError("LAYER_NOT_MANAGED")
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    entry = layers.get(lid)
    if not entry:
        raise ValueError("LAYER_NOT_FOUND")
    data = entry.get("data") or {}
    identify = entry.get("identify") or {}
    export_cfg = data.get("export") or {}
    export_columns = data.get("export_columns") or export_cfg.get("columns") or []
    return {
        "layer_id": lid,
        "label": entry.get("label") or lid,
        "group_id": find_layer_group_id(raw, lid) or "",
        "geometry": entry.get("geometry") or "polygon",
        "style_preset": entry.get("style_preset") or "",
        "style": entry.get("style") or {},
        "data": {
            "table": data.get("table") or "",
            "mun_filter": data.get("mun_filter", "cve_mun"),
            "export_columns": list(export_columns) if export_columns else [],
        },
        "capabilities": entry.get("capabilities") or {},
        "identify": {
            "title": identify.get("title") or entry.get("label") or lid,
            "fields": _normalize_identify_fields(identify.get("fields")),
        },
        "labels": _normalize_labels_for_admin(entry),
        "denue": {
            "codigo_act": list((data.get("filter") or {}).get("codigo_act") or []),
            "use_template": (identify.get("template") == "denue"),
        }
        if str(data.get("table") or "").lower() == "c_denue"
        else None,
        "overlay_key": entry.get("overlay_key"),
        "checkbox_id": entry.get("checkbox_id"),
    }


def validate_new_layer(payload: Dict[str, Any]) -> Dict[str, Any]:
    icons = [i["key"] for i in load_icons_meta()]
    warnings = validate_layer_payload(payload, icons)
    layer_id = slug_layer_id(payload.get("layer_id") or "")
    if layer_id and layer_id in catalog_layer_ids():
        warnings.append(f"layer_id '{layer_id}' ya existe en el catálogo")
    return {"ok": not warnings, "warnings": warnings}


def create_layer_from_payload(payload: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    icons = [i["key"] for i in load_icons_meta()]
    warnings = validate_layer_payload(payload, icons)
    if warnings:
        raise ValueError("VALIDATION:" + "|".join(warnings))

    layer_id = payload.get("layer_id") or ""
    group_id = payload.get("group_id") or ""
    entry = build_layer_entry(payload)

    catalog = load_catalog_mutable()
    catalog, lid, before = merge_layer_entry(catalog, layer_id, entry, group_id)
    save_catalog(catalog)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {ADMIN_SCHEMA}.catalog_audit
                    (user_id, action, layer_id, before_json, after_json)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, "create_layer", lid, None, json.dumps(entry)),
            )
            table_name = (entry.get("data") or {}).get("table") or ""
            cur.execute(
                f"""
                INSERT INTO {ADMIN_SCHEMA}.layer_publications
                    (layer_id, table_name, published_by, catalog_snapshot)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (layer_id) DO UPDATE
                   SET table_name = EXCLUDED.table_name,
                       published_by = EXCLUDED.published_by,
                       published_at = NOW(),
                       catalog_snapshot = EXCLUDED.catalog_snapshot
                """,
                (lid, table_name, user_id, json.dumps(entry)),
            )

    return {"layer_id": lid, "warnings": warnings, "entry": entry}


def update_layer_from_payload(layer_id: str, payload: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    lid = slug_layer_id(layer_id)
    if lid not in _managed_layer_ids():
        raise ValueError("LAYER_NOT_MANAGED")
    icons = [i["key"] for i in load_icons_meta()]
    warnings = validate_layer_update_payload(payload, lid, icons)
    if warnings:
        raise ValueError("VALIDATION:" + "|".join(warnings))

    group_id = payload.get("group_id") or ""
    entry = build_layer_entry({**payload, "layer_id": lid})

    catalog = load_catalog_mutable()
    catalog, saved_id, before = replace_layer_entry(catalog, lid, entry, group_id)
    save_catalog(catalog)

    record_audit(user_id, "update_layer", saved_id, before, entry)
    with get_db() as conn:
        with conn.cursor() as cur:
            table_name = (entry.get("data") or {}).get("table") or ""
            cur.execute(
                f"""
                UPDATE {ADMIN_SCHEMA}.layer_publications
                   SET table_name = %s,
                       published_by = %s,
                       published_at = NOW(),
                       catalog_snapshot = %s
                 WHERE layer_id = %s
                """,
                (table_name, user_id, json.dumps(entry), saved_id),
            )

    return {"layer_id": saved_id, "warnings": warnings, "entry": entry}


def delete_managed_layer(layer_id: str, user_id: int) -> Dict[str, Any]:
    lid = slug_layer_id(layer_id)
    if lid not in _managed_layer_ids():
        raise ValueError("LAYER_NOT_MANAGED")

    catalog = load_catalog_mutable()
    catalog, saved_id, before = delete_layer_entry(catalog, lid)
    save_catalog(catalog)

    record_audit(user_id, "delete_layer", saved_id, before, None)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {ADMIN_SCHEMA}.layer_publications WHERE layer_id = %s",
                (saved_id,),
            )

    return {"layer_id": saved_id, "deleted": True}


def record_audit(
    user_id: int,
    action: str,
    layer_id: str,
    before_json: Optional[Dict[str, Any]],
    after_json: Optional[Dict[str, Any]],
) -> None:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {ADMIN_SCHEMA}.catalog_audit
                    (user_id, action, layer_id, before_json, after_json)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    action,
                    layer_id,
                    json.dumps(before_json) if before_json else None,
                    json.dumps(after_json) if after_json else None,
                ),
            )
