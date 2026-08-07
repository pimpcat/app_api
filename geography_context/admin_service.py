"""Admin: meta (tablas texto + capas Visor), columnas, guardar catálogo."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from database import get_db
from geography_context.catalog_loader import (
    assert_sql_ident,
    geography_catalog_path,
    load_geography_catalog_raw,
)
from geography_context.catalog_writer import save_geography_catalog
from geography_context.services import invalidate_texto_cache
from tables import SCHEMA
from visor_catalog_admin_service import _editable_studio_layer_ids
from visor_catalog_loader import load_visor_catalog_raw


def list_text_tables() -> List[Dict[str, Any]]:
    """Tablas atlas.* con al menos una columna de texto (candidatas a ficha)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.table_name,
                       COUNT(*) FILTER (
                         WHERE c.data_type IN (
                           'text', 'character varying', 'character', 'varchar', 'char'
                         )
                       ) AS text_cols
                  FROM information_schema.tables t
                  JOIN information_schema.columns c
                    ON c.table_schema = t.table_schema
                   AND c.table_name = t.table_name
                 WHERE t.table_schema = %s
                   AND t.table_type = 'BASE TABLE'
                 GROUP BY t.table_name
                HAVING COUNT(*) FILTER (
                         WHERE c.data_type IN (
                           'text', 'character varying', 'character', 'varchar', 'char'
                         )
                       ) > 0
                 ORDER BY t.table_name
                """,
                (SCHEMA,),
            )
            rows = cur.fetchall()
    return [
        {
            "table": r["table_name"],
            "label": f"{SCHEMA}.{r['table_name']}",
            "text_columns": int(r.get("text_cols") or 0),
        }
        for r in rows
        if r.get("table_name")
    ]


def list_table_columns(table_name: str) -> List[Dict[str, str]]:
    table = assert_sql_ident(table_name, label="table")
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
    return [
        {"name": r["column_name"], "data_type": r["data_type"]}
        for r in rows
        if r.get("column_name")
    ]


def list_visor_layers_for_picker() -> List[Dict[str, Any]]:
    """Todas las capas del Visor Catalog; managed=false = legacy/núcleo."""
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    managed: Set[str] = set()
    try:
        managed = {x.lower() for x in _editable_studio_layer_ids()}
    except Exception:
        managed = set()
    out: List[Dict[str, Any]] = []
    if not isinstance(layers, dict):
        return out
    for layer_id, entry in layers.items():
        if not isinstance(entry, dict):
            continue
        data = entry.get("data") or {}
        is_managed = layer_id.lower() in managed
        out.append(
            {
                "layer_id": layer_id,
                "label": entry.get("label") or layer_id,
                "table": data.get("table") or "",
                "geometry": entry.get("geometry") or "",
                "style_preset": entry.get("style_preset") or "",
                "managed": is_managed,
                "legacy": not is_managed,
            }
        )
    out.sort(key=lambda x: ((x.get("label") or "").lower(), x.get("layer_id") or ""))
    return out


def admin_meta() -> Dict[str, Any]:
    cat = load_geography_catalog_raw()
    return {
        "ok": True,
        "catalog_path": str(geography_catalog_path()),
        "tables": list_text_tables(),
        "visor_layers": list_visor_layers_for_picker(),
        "tabs_count": len(cat.get("tabs") or []),
        "menu": cat.get("menu") or {},
        "layout": cat.get("layout") or {},
        "defaults": cat.get("defaults") or {},
    }


def get_admin_catalog() -> Dict[str, Any]:
    data = load_geography_catalog_raw()
    return {"ok": True, "catalog": data, "path": str(geography_catalog_path())}


def save_admin_catalog(
    catalog: Dict[str, Any], *, user_id: Optional[int] = None
) -> Dict[str, Any]:
    # Validar que layers referenciadas existan en Visor (aviso suave → error duro)
    visor_ids = {x["layer_id"] for x in list_visor_layers_for_picker()}
    for tab in catalog.get("tabs") or []:
        if not isinstance(tab, dict):
            continue
        for lid in tab.get("layers") or []:
            if str(lid).strip() and str(lid).strip() not in visor_ids:
                raise ValueError(
                    f"Pestaña {tab.get('id')}: capa Visor desconocida '{lid}'"
                )
    result = save_geography_catalog(catalog, user_id=user_id)
    invalidate_texto_cache()
    return result
