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
    create_group_entry,
    delete_group_entry,
    delete_layer_entry,
    find_layer_group_id,
    load_catalog_mutable,
    merge_layer_entry,
    replace_layer_entry,
    save_catalog,
    slug_group_id,
    update_group_label_entry,
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


def fetch_postgis_vector_table_ids() -> List[str]:
    """Tablas vectoriales c_* / v_c_* en PostGIS (aunque Martin aún no las liste)."""
    settings = get_settings()
    schema = settings.get("schema") or "atlas"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT f_table_name AS table_name
                  FROM geometry_columns
                 WHERE f_table_schema = %s
                   AND (f_table_name LIKE 'c\\_%' ESCAPE '\\'
                        OR f_table_name LIKE 'v\\_c\\_%' ESCAPE '\\')
                 ORDER BY 1
                """,
                (schema,),
            )
            rows = cur.fetchall()
    return [str(r["table_name"]).strip() for r in rows if r.get("table_name")]


def list_publishable_tables() -> List[Dict[str, Any]]:
    """Tablas disponibles para publicar: en Martin y/o PostGIS, excluyendo las ya en catalog.json."""
    try:
        martin_ids = {t.lower() for t in fetch_martin_table_ids()}
    except RuntimeError:
        martin_ids = set()
    try:
        postgis_ids = {t.lower() for t in fetch_postgis_vector_table_ids()}
    except Exception:
        postgis_ids = set()
    used = catalog_table_names()
    out: List[Dict[str, Any]] = []
    for table in sorted(martin_ids | postgis_ids):
        if table in used:
            continue
        in_martin = table in martin_ids
        out.append(
            {
                "table": table,
                "label": table,
                "in_martin": in_martin,
                "in_postgis": table in postgis_ids,
                "needs_martin_restart": not in_martin and table in postgis_ids,
            }
        )
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
    settings = get_settings()
    group_list = [
        {
            "id": g.get("id"),
            "label": g.get("label") or g.get("id"),
            "layer_count": len(g.get("layers") or []),
        }
        for g in (raw.get("groups") or [])
        if isinstance(g, dict) and g.get("id")
    ]
    return {
        "presets": load_preset_meta(),
        "icons": load_icons_meta(),
        "groups": group_list,
        "cluster_presets": [
            {"id": "standard", "label": "Equilibrado", "hint": "Agrupación media; se desglosa cerca de zoom 14"},
            {"id": "compact", "label": "Compacto", "hint": "Clusters más pequeños; puntos sueltos antes (zoom 15)"},
            {"id": "wide", "label": "Amplio", "hint": "Menos grupos; útil con muchos puntos dispersos"},
            {"id": "sparse", "label": "Discreto", "hint": "Solo agrupa con 3+ puntos; mapa más limpio"},
        ],
        "phase": 3,
        "db_schema": settings.get("schema") or "atlas",
    }


def list_catalog_groups() -> List[Dict[str, Any]]:
    raw = load_visor_catalog_raw()
    out: List[Dict[str, Any]] = []
    for grp in raw.get("groups") or []:
        if not isinstance(grp, dict):
            continue
        gid = grp.get("id")
        if not gid:
            continue
        layers = grp.get("layers") or []
        out.append(
            {
                "id": str(gid),
                "label": str(grp.get("label") or gid),
                "layer_count": len(layers) if isinstance(layers, list) else 0,
                "collapsible": grp.get("collapsible", True),
                "collapsed": grp.get("collapsed", False),
            }
        )
    return out


def create_group_from_payload(payload: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    label = (payload.get("label") or "").strip()
    if len(label) < 2:
        raise ValueError("VALIDATION:Etiqueta del grupo requerida (mín. 2 caracteres)")
    raw_id = (payload.get("group_id") or "").strip()
    group_id = raw_id or slug_group_id(label)
    if len(group_id) < 2:
        raise ValueError("VALIDATION:Identificador de grupo inválido")

    catalog = load_catalog_mutable()
    catalog, gid = create_group_entry(catalog, group_id, label)
    save_catalog(catalog)

    entry = next(
        (g for g in (catalog.get("groups") or []) if isinstance(g, dict) and g.get("id") == gid),
        {"id": gid, "label": label, "layers": []},
    )
    record_audit(user_id, "create_group", gid, None, dict(entry))
    return {"group_id": gid, "label": label, "layer_count": 0}


def delete_catalog_group(group_id: str, user_id: int) -> Dict[str, Any]:
    gid = slug_group_id(group_id)
    catalog = load_catalog_mutable()
    catalog, saved_id, before = delete_group_entry(catalog, gid)
    save_catalog(catalog)
    record_audit(user_id, "delete_group", saved_id, before, {"label": (before or {}).get("label") or saved_id})
    return {"group_id": saved_id, "deleted": True}


def update_catalog_group_label(group_id: str, label: str, user_id: int) -> Dict[str, Any]:
    clean = (label or "").strip()
    if len(clean) < 2:
        raise ValueError("VALIDATION:Etiqueta del grupo requerida (mín. 2 caracteres)")
    catalog = load_catalog_mutable()
    catalog, saved_id, before = update_group_label_entry(catalog, group_id, clean)
    save_catalog(catalog)
    after = next(
        (g for g in (catalog.get("groups") or []) if isinstance(g, dict) and g.get("id") == saved_id),
        None,
    )
    record_audit(user_id, "update_group", saved_id, before, after)
    return {"group_id": saved_id, "label": clean}


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


def _studio_layer_ids_from_audit() -> set[str]:
    """Capas cuya última acción de auditoría es alta o edición (no despublicación)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ON (LOWER(layer_id))
                       LOWER(layer_id) AS layer_id,
                       action
                  FROM {ADMIN_SCHEMA}.catalog_audit
                 WHERE layer_id IS NOT NULL
                   AND action IN ('create_layer', 'update_layer', 'delete_layer')
                 ORDER BY LOWER(layer_id), created_at DESC, id DESC
                """
            )
            rows = cur.fetchall()
    out: set[str] = set()
    for row in rows:
        action = str(row.get("action") or "").strip().lower()
        lid = str(row.get("layer_id") or "").strip().lower()
        if lid and action in ("create_layer", "update_layer"):
            out.add(lid)
    return out


def _upsert_layer_publication(
    cur: Any,
    layer_id: str,
    table_name: str,
    user_id: int,
    entry: Dict[str, Any],
) -> None:
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
        (layer_id, table_name, user_id, json.dumps(entry)),
    )


def _repair_orphan_publications() -> None:
    """Re-enlaza capas gestionables que faltan en layer_publications."""
    editable = _studio_layer_ids_from_audit()
    missing = editable - _managed_layer_ids()
    if not missing:
        return
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    with get_db() as conn:
        with conn.cursor() as cur:
            for lid in sorted(missing):
                entry = layers.get(lid)
                catalog_key = lid
                if not isinstance(entry, dict):
                    for key, val in layers.items():
                        if str(key).lower() == lid:
                            entry = val
                            catalog_key = str(key)
                            break
                if not isinstance(entry, dict):
                    continue
                table_name = (entry.get("data") or {}).get("table") or ""
                _upsert_layer_publication(cur, catalog_key, table_name, 0, entry)


def _editable_studio_layer_ids() -> set[str]:
    _repair_orphan_publications()
    return _managed_layer_ids() | _studio_layer_ids_from_audit()


def list_managed_layers() -> List[Dict[str, Any]]:
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    managed = _editable_studio_layer_ids()
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
            "offset": [0, 0],
            "source": "centroid" if geometry == "polygon" else "",
        }
    off = labels.get("offset")
    offset = [0, 0]
    if isinstance(off, (list, tuple)) and len(off) >= 2:
        try:
            offset = [float(off[0]), float(off[1])]
        except (TypeError, ValueError):
            offset = [0, 0]
    return {
        "enabled": True,
        "field": str(labels.get("field") or "").strip(),
        "minzoom": labels.get("minzoom", default_minz),
        "above_icon": labels.get("above_icon", True),
        "color": labels.get("color") or "#2c3e50",
        "offset": offset,
        "source": str(labels.get("source") or ("centroid" if geometry == "polygon" else "")),
    }


def get_layer_admin_detail(layer_id: str) -> Dict[str, Any]:
    lid = slug_layer_id(layer_id)
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    entry = layers.get(lid)
    if not entry:
        raise ValueError("LAYER_NOT_FOUND")
    if lid not in _editable_studio_layer_ids():
        raise ValueError("LAYER_NOT_MANAGED")
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
            "export_kml_name_field": str(export_cfg.get("kml_name_field") or "").strip(),
            "filter": dict(data.get("filter") or {}) if data.get("filter") else {},
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
        "search": _normalize_search_for_admin(entry),
        "spatial_analysis": _normalize_spatial_for_admin(entry),
        "tabular": _normalize_tabular_for_admin(entry),
    }


def _normalize_tabular_for_admin(entry: Dict[str, Any]) -> Dict[str, Any]:
    caps = entry.get("capabilities") or {}
    tabular = entry.get("tabular") or {}
    if not caps.get("tabular"):
        return {"enabled": False, "columns": []}
    if not isinstance(tabular, dict):
        tabular = {}
    columns: List[Dict[str, str]] = []
    for item in tabular.get("columns") or []:
        if not isinstance(item, dict):
            continue
        field = item.get("field") or item.get("column")
        if not field:
            continue
        columns.append(
            {
                "field": str(field),
                "label": str(item.get("label") or item.get("etiqueta") or field),
            }
        )
    return {
        "enabled": True,
        "preset": str(tabular.get("preset") or ""),
        "columns": columns,
    }


def _normalize_spatial_for_admin(entry: Dict[str, Any]) -> Dict[str, Any]:
    caps = entry.get("capabilities") or {}
    spatial = entry.get("spatial_analysis") or {}
    geometry = str(entry.get("geometry") or "polygon")
    if not caps.get("spatial_analysis"):
        return {
            "enabled": False,
            "modo": _default_spatial_modo_admin(geometry),
            "detail_table": False,
            "fields": [],
            "detail_columns": [],
            "ui": {},
        }
    if not isinstance(spatial, dict):
        spatial = {}
    modo = str(spatial.get("modo") or _default_spatial_modo_admin(geometry))
    fields: List[Dict[str, str]] = []
    for section in spatial.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for field in section.get("campos") or []:
            if not isinstance(field, dict):
                continue
            col = field.get("columna") or field.get("column")
            if not col:
                continue
            fields.append(
                {
                    "columna": str(col),
                    "etiqueta": str(field.get("etiqueta") or field.get("label") or col),
                    "agregacion": str(field.get("agregacion") or "sum"),
                }
            )
    detail_cols: List[Dict[str, str]] = []
    for field in spatial.get("detail_columns") or []:
        if not isinstance(field, dict):
            continue
        col = field.get("columna") or field.get("column")
        if not col:
            continue
        detail_cols.append(
            {
                "columna": str(col),
                "etiqueta": str(field.get("etiqueta") or field.get("label") or col),
            }
        )
    ui = spatial.get("ui") if isinstance(spatial.get("ui"), dict) else {}
    return {
        "enabled": True,
        "modo": modo,
        "detail_table": bool(spatial.get("detail_table")),
        "fields": fields,
        "detail_columns": detail_cols,
        "ui": {
            "unidad_registro": str(ui.get("unidad_registro") or ""),
            "empty_msg": str(ui.get("empty_msg") or ""),
        },
    }


def _default_spatial_modo_admin(geometry: str) -> str:
    return "conteo" if (geometry or "").strip().lower() == "point" else "agregacion"


def _normalize_search_for_admin(entry: Dict[str, Any]) -> Dict[str, Any]:
    search = entry.get("search") or {}
    label = entry.get("label") or ""
    if not isinstance(search, dict) or not search.get("enabled"):
        return {
            "enabled": False,
            "tipo": label,
            "name_column": "",
            "id_column": "cvegeo",
            "search_columns": [],
            "scope": "",
            "geom_mode": "",
        }
    name_col = str(search.get("name_column") or "").strip()
    return {
        "enabled": True,
        "tipo": str(search.get("tipo") or label),
        "name_column": name_col,
        "id_column": str(search.get("id_column") or "cvegeo"),
        "search_columns": list(search.get("search_columns") or ([name_col] if name_col else [])),
        "scope": str(search.get("scope") or ""),
        "geom_mode": str(search.get("geom_mode") or ""),
    }


def validate_new_layer(payload: Dict[str, Any]) -> Dict[str, Any]:
    icons = [i["key"] for i in load_icons_meta()]
    warnings = validate_layer_payload(payload, icons)
    layer_id = slug_layer_id(payload.get("layer_id") or "")
    if layer_id and layer_id in catalog_layer_ids():
        warnings.append(f"layer_id '{layer_id}' ya existe en el catálogo")
    table = ((payload.get("data") or {}).get("table") or "").strip().lower()
    if table and table in catalog_table_names():
        warnings.append(f"La tabla '{table}' ya está registrada en otra capa del catálogo")
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

    record_audit(user_id, "create_layer", lid, before, entry)
    with get_db() as conn:
        with conn.cursor() as cur:
            table_name = (entry.get("data") or {}).get("table") or ""
            _upsert_layer_publication(cur, lid, table_name, user_id, entry)

    return {"layer_id": lid, "warnings": warnings, "entry": entry}


def update_layer_from_payload(layer_id: str, payload: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    lid = slug_layer_id(layer_id)
    raw = load_visor_catalog_raw()
    if lid not in (raw.get("layers") or {}):
        raise ValueError("LAYER_NOT_FOUND")
    if lid not in _editable_studio_layer_ids():
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
            _upsert_layer_publication(cur, saved_id, table_name, user_id, entry)

    return {"layer_id": saved_id, "warnings": warnings, "entry": entry}


def delete_managed_layer(layer_id: str, user_id: int) -> Dict[str, Any]:
    lid = slug_layer_id(layer_id)
    if lid not in _editable_studio_layer_ids():
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
    layer_id: Optional[str],
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


AUDIT_ACTION_LABELS: Dict[str, str] = {
    "create_layer": "Publicó capa",
    "update_layer": "Editó capa",
    "delete_layer": "Despublicó capa",
    "create_group": "Creó grupo de capas",
    "update_group": "Renombró grupo de capas",
    "delete_group": "Eliminó grupo de capas",
    "import_shp": "Importó shapefile",
    "create_indexes": "Creó índices PostgreSQL",
    "upload_icon": "Registró icono SVG",
    "create_admin_user": "Creó usuario admin",
    "update_admin_user": "Actualizó usuario admin",
    "change_password": "Cambió su contraseña",
    "reset_password": "Restableció contraseña de usuario",
}


def _audit_summary(action: str, layer_id: Optional[str], after_json: Optional[Dict[str, Any]]) -> str:
    after = after_json if isinstance(after_json, dict) else {}
    if action == "import_shp":
        table = after.get("table") or layer_id or "—"
        n = after.get("feature_count")
        fname = after.get("filename") or ""
        extra = f" ({n} features)" if n is not None else ""
        return f"Tabla {table}{extra}" + (f" · {fname}" if fname else "")
    if action == "create_indexes":
        table = after.get("table") or layer_id or "—"
        created = after.get("created") or []
        skipped = after.get("skipped") or []
        parts = [f"Tabla {table}"]
        if created:
            cols = ", ".join(f"{c.get('column')}" for c in created[:4])
            suffix = "…" if len(created) > 4 else ""
            parts.append(f"creados: {cols}{suffix}")
        if skipped and not created:
            parts.append(f"{len(skipped)} ya existían")
        return " · ".join(parts)
    if action in ("create_layer", "update_layer", "delete_layer"):
        label = after.get("label") or layer_id or "—"
        table = (after.get("data") or {}).get("table")
        if table:
            return f"{label} ({table})"
        return str(label)
    if action in ("create_group", "update_group", "delete_group"):
        return str(after.get("label") or layer_id or "—")
    if layer_id:
        return str(layer_id)
    return AUDIT_ACTION_LABELS.get(action, action)


def record_shp_import_audit(
    user_id: int,
    *,
    table: str,
    filename: str,
    feature_count: int,
    geometry: str,
) -> None:
    record_audit(
        user_id,
        "import_shp",
        table,
        None,
        {
            "table": table,
            "filename": filename,
            "feature_count": feature_count,
            "geometry": geometry,
        },
    )


def record_indexes_audit(user_id: int, table: str, result: Dict[str, Any]) -> None:
    record_audit(
        user_id,
        "create_indexes",
        table,
        None,
        {
            "table": table,
            "created": result.get("created") or [],
            "skipped": result.get("skipped") or [],
            "errors": result.get("errors") or [],
        },
    )


def list_catalog_audit(
    *,
    limit: int = 50,
    offset: int = 0,
    action: Optional[str] = None,
    layer_id: Optional[str] = None,
    table: Optional[str] = None,
) -> Dict[str, Any]:
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    clauses = ["1=1"]
    params: List[Any] = []
    if action:
        clauses.append("a.action = %s")
        params.append(action.strip())
    if layer_id:
        clauses.append("LOWER(COALESCE(a.layer_id, '')) = LOWER(%s)")
        params.append(layer_id.strip())
    if table:
        t = table.strip().lower()
        clauses.append(
            "(LOWER(COALESCE(a.layer_id, '')) = %s OR LOWER(COALESCE(a.after_json->>'table', '')) = %s)"
        )
        params.extend([t, t])
    where_sql = " AND ".join(clauses)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)::int AS n
                  FROM {ADMIN_SCHEMA}.catalog_audit a
                 WHERE {where_sql}
                """,
                params,
            )
            total = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute(
                f"""
                SELECT a.id,
                       a.user_id,
                       a.action,
                       a.layer_id,
                       a.before_json,
                       a.after_json,
                       a.created_at,
                       u.username,
                       u.display_name
                  FROM {ADMIN_SCHEMA}.catalog_audit a
                  LEFT JOIN {ADMIN_SCHEMA}.users u ON u.id = a.user_id
                 WHERE {where_sql}
                 ORDER BY a.created_at DESC, a.id DESC
                 LIMIT %s OFFSET %s
                """,
                [*params, lim, off],
            )
            rows = cur.fetchall()
    entries: List[Dict[str, Any]] = []
    for row in rows:
        act = str(row.get("action") or "")
        after = row.get("after_json")
        if isinstance(after, str):
            try:
                after = json.loads(after)
            except json.JSONDecodeError:
                after = None
        lid = row.get("layer_id")
        entries.append(
            {
                "id": row.get("id"),
                "action": act,
                "action_label": AUDIT_ACTION_LABELS.get(act, act),
                "layer_id": lid,
                "summary": _audit_summary(act, lid, after if isinstance(after, dict) else None),
                "user_id": row.get("user_id"),
                "username": row.get("username"),
                "display_name": row.get("display_name"),
                "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
                "after": after if isinstance(after, dict) else None,
            }
        )
    return {"total": total, "limit": lim, "offset": off, "entries": entries}
