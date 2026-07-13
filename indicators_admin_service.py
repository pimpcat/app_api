"""Servicio admin del catálogo de indicadores (Indicators Studio, Fase 11)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from config_json_errors import dump_json_object, verify_json_roundtrip

from auth.users import ADMIN_SCHEMA
from column_resolver import clear_column_cache
from database import get_db
from indicator_profiles import list_handlers, list_profiles
from indicators_catalog_loader import (
    indicators_catalog_path,
    invalidate_indicators_catalog_cache,
    load_indicators_catalog_raw,
)
from presentation_presets_loader import (
    invalidate_presentation_presets_cache,
    load_presentation_presets_raw,
)
from tables import SCHEMA, T_MUN, T_TAB_MUNICIPAL, T_TAB_NACIONAL
from visor_catalog_admin_service import record_audit

# Acciones de Indicators Studio en atlas_admin.catalog_audit (misma tabla que Visor).
INDICATOR_AUDIT_ACTIONS = (
    "create_indicator",
    "update_indicator",
    "delete_indicator",
    "replace_catalog",
)

INDICATOR_AUDIT_ACTION_LABELS: Dict[str, str] = {
    "create_indicator": "Creó indicador",
    "update_indicator": "Publicó cambios",
    "delete_indicator": "Eliminó indicador",
    "replace_catalog": "Reemplazó catálogo",
}

_TABLE_MAP = {
    "tab_municipal": T_TAB_MUNICIPAL,
    "tab_nacional": T_TAB_NACIONAL,
    "c_mun": T_MUN,
}


def admin_meta() -> Dict[str, Any]:
    catalog = load_indicators_catalog_raw()
    presets = load_presentation_presets_raw()
    return {
        "ok": True,
        "groups": catalog.get("groups") or [],
        "profiles": list_profiles(),
        "handlers": list_handlers(),
        "presets": [
            {
                "id": p.get("id"),
                "label": p.get("label"),
                "status": p.get("status"),
                "response_profiles": p.get("response_profiles") or [],
                "config": p.get("config") or {},
            }
            for p in (presets.get("presets") or [])
            if p.get("status") == "implemented"
        ],
        "tables": [
            {"id": "tab_municipal", "label": "atlas.tab_municipal"},
            {"id": "tab_nacional", "label": "atlas.tab_nacional"},
            {"id": "c_mun", "label": "atlas.c_mun"},
        ],
        "catalog_path": indicators_catalog_path(),
        "indicators_count": len(catalog.get("indicators") or []),
    }


def get_admin_catalog() -> Dict[str, Any]:
    data = load_indicators_catalog_raw()
    return {"ok": True, "catalog": data, "path": indicators_catalog_path()}


def list_table_columns(table_key: str) -> Dict[str, Any]:
    table = _TABLE_MAP.get((table_key or "").strip())
    if not table:
        raise ValueError(f"Tabla desconocida: {table_key}")
    clear_column_cache(SCHEMA, table)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type
                  FROM information_schema.columns
                 WHERE table_schema = %s AND table_name = %s
                 ORDER BY ordinal_position
                """,
                (SCHEMA, table),
            )
            rows = cur.fetchall()
    columns = [
        {"name": r["column_name"], "data_type": r["data_type"]}
        for r in rows
        if r.get("column_name")
    ]
    return {"ok": True, "table": table_key, "schema": SCHEMA, "columns": columns}


def _validate_indicator_entry(ind: Dict[str, Any], groups: List[Dict[str, Any]]) -> None:
    if not ind.get("id"):
        raise ValueError("Indicador sin id")
    iid = str(ind["id"]).strip()
    if not iid.replace("_", "").isalnum() or not iid[0].isalpha():
        raise ValueError(f"id inválido: {iid}")
    if not ind.get("label"):
        raise ValueError(f"{iid}: falta label")
    gid = ind.get("group_id")
    group_ids = {g.get("id") for g in groups}
    if gid and gid not in group_ids:
        raise ValueError(f"{iid}: group_id desconocido '{gid}'")
    api = ind.get("api") or {}
    profile = api.get("response_profile")
    if not profile:
        raise ValueError(f"{iid}: falta api.response_profile")
    if profile not in list_profiles():
        raise ValueError(f"{iid}: response_profile desconocido '{profile}'")
    handler = (api.get("handler") or "").strip()
    if handler and handler not in list_handlers():
        raise ValueError(f"{iid}: handler desconocido '{handler}'")
    pres = ind.get("presentation") or {}
    if not pres.get("template"):
        raise ValueError(f"{iid}: falta presentation.template")
    fields = ind.get("fields") or []
    if not fields:
        raise ValueError(f"{iid}: se requiere al menos un field")


def _catalog_audit_snapshot(catalog: Dict[str, Any]) -> Dict[str, Any]:
    indicators = catalog.get("indicators") or []
    return {
        "indicators_count": len(indicators),
        "indicator_ids": [ind.get("id") for ind in indicators if isinstance(ind, dict)],
        "groups": [
            {"id": g.get("id"), "label": g.get("label")}
            for g in (catalog.get("groups") or [])
            if isinstance(g, dict)
        ],
    }


def _indicator_audit_snapshot(ind: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(ind, dict):
        return None
    # Copia profunda para no mutar el catálogo en memoria.
    return json.loads(json.dumps(ind))


def _indicator_audit_summary(
    action: str, indicator_id: Optional[str], after_json: Optional[Dict[str, Any]]
) -> str:
    after = after_json if isinstance(after_json, dict) else {}
    if action == "replace_catalog":
        n = after.get("indicators_count")
        return f"Catálogo completo ({n} indicadores)" if n is not None else "Catálogo completo"
    label = after.get("label") or indicator_id or "—"
    group = after.get("group_id")
    if group:
        return f"{label} · {group}"
    return str(label)


def _write_catalog_file(catalog: Dict[str, Any]) -> Dict[str, Any]:
    """Valida y escribe catalog.json (sin auditoría)."""
    if not isinstance(catalog, dict):
        raise ValueError("Catálogo inválido")
    groups = catalog.get("groups") or []
    indicators = catalog.get("indicators") or []
    if not groups:
        raise ValueError("groups vacío")
    if not indicators:
        raise ValueError("indicators vacío")

    seen = set()
    for ind in indicators:
        if not isinstance(ind, dict):
            raise ValueError("indicador debe ser objeto")
        _validate_indicator_entry(ind, groups)
        iid = ind["id"]
        if iid in seen:
            raise ValueError(f"id duplicado: {iid}")
        seen.add(iid)

    verify_json_roundtrip(catalog)

    path = Path(indicators_catalog_path())
    backup = path.with_suffix(".json.bak")
    if path.is_file():
        backup.write_bytes(path.read_bytes())

    text = dump_json_object(catalog)
    path.write_text(text, encoding="utf-8")
    invalidate_indicators_catalog_cache()
    invalidate_presentation_presets_cache()
    return {
        "ok": True,
        "path": str(path),
        "indicators_count": len(indicators),
        "backup": str(backup),
    }


def save_admin_catalog(
    catalog: Dict[str, Any], *, user_id: Optional[int] = None
) -> Dict[str, Any]:
    before_catalog = load_indicators_catalog_raw()
    before_snap = _catalog_audit_snapshot(before_catalog)
    result = _write_catalog_file(catalog)
    after_snap = _catalog_audit_snapshot(catalog)
    if user_id is not None:
        record_audit(int(user_id), "replace_catalog", None, before_snap, after_snap)
    return result


def upsert_indicator(
    entry: Dict[str, Any], *, user_id: Optional[int] = None
) -> Dict[str, Any]:
    catalog = load_indicators_catalog_raw()
    catalog = json.loads(json.dumps(catalog))
    groups = catalog.get("groups") or []
    _validate_indicator_entry(entry, groups)
    indicators = catalog.get("indicators") or []
    iid = entry["id"]
    before_entry: Optional[Dict[str, Any]] = None
    found = False
    for i, ind in enumerate(indicators):
        if ind.get("id") == iid:
            before_entry = ind
            indicators[i] = entry
            found = True
            break
    if not found:
        indicators.append(entry)
    catalog["indicators"] = indicators
    result = _write_catalog_file(catalog)
    result["indicator_id"] = iid
    result["created"] = not found
    if user_id is not None:
        action = "create_indicator" if not found else "update_indicator"
        record_audit(
            int(user_id),
            action,
            iid,
            _indicator_audit_snapshot(before_entry),
            _indicator_audit_snapshot(entry),
        )
    return result


def delete_indicator(
    indicator_id: str, *, user_id: Optional[int] = None
) -> Dict[str, Any]:
    catalog = load_indicators_catalog_raw()
    catalog = json.loads(json.dumps(catalog))
    iid = (indicator_id or "").strip()
    before_entry: Optional[Dict[str, Any]] = None
    kept: List[Dict[str, Any]] = []
    for ind in catalog.get("indicators") or []:
        if ind.get("id") == iid:
            before_entry = ind
        else:
            kept.append(ind)
    if before_entry is None:
        raise ValueError(f"Indicador no encontrado: {iid}")
    catalog["indicators"] = kept
    result = _write_catalog_file(catalog)
    result["deleted"] = iid
    if user_id is not None:
        record_audit(
            int(user_id),
            "delete_indicator",
            iid,
            _indicator_audit_snapshot(before_entry),
            {"id": iid, "label": (before_entry or {}).get("label") or iid},
        )
    return result


def list_indicators_audit(
    *,
    limit: int = 50,
    offset: int = 0,
    action: Optional[str] = None,
    indicator_id: Optional[str] = None,
) -> Dict[str, Any]:
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    clauses = ["a.action = ANY(%s)"]
    params: List[Any] = [list(INDICATOR_AUDIT_ACTIONS)]
    if action:
        act = action.strip()
        if act not in INDICATOR_AUDIT_ACTIONS:
            raise ValueError(f"Acción de auditoría desconocida: {act}")
        clauses.append("a.action = %s")
        params.append(act)
    if indicator_id:
        clauses.append("LOWER(COALESCE(a.layer_id, '')) = LOWER(%s)")
        params.append(indicator_id.strip())
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
        iid = row.get("layer_id")
        entries.append(
            {
                "id": row.get("id"),
                "action": act,
                "action_label": INDICATOR_AUDIT_ACTION_LABELS.get(act, act),
                "indicator_id": iid,
                "summary": _indicator_audit_summary(
                    act, iid, after if isinstance(after, dict) else None
                ),
                "user_id": row.get("user_id"),
                "username": row.get("username"),
                "display_name": row.get("display_name"),
                "created_at": row.get("created_at").isoformat()
                if row.get("created_at")
                else None,
            }
        )
    return {"total": total, "limit": lim, "offset": off, "entries": entries}
