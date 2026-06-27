"""Sincroniza columnas MVT en martin.yaml para capas publicadas desde Visor Studio."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


def martin_config_path() -> Path:
    env = (os.getenv("MARTIN_CONFIG_PATH") or "").strip()
    candidates = [Path(env)] if env else []
    candidates.extend(
        [
            Path("/martin.yaml"),
            Path(__file__).resolve().parent.parent / "martin.yaml",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[-1]


def _field_name(item: Any) -> Optional[str]:
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, dict):
        col = item.get("column") or item.get("field") or item.get("name")
        if col and str(col).strip():
            return str(col).strip()
    return None


def collect_mvt_columns(payload: Dict[str, Any]) -> List[str]:
    """Columnas que deben ir en properties del MVT (identify + etiquetas + filtros)."""
    cols: Set[str] = {"gid"}
    data = payload.get("data") or {}
    table = str(data.get("table") or "").strip()
    if not table:
        return sorted(cols)

    mun = data.get("mun_filter")
    if mun and mun is not False:
        cols.add(str(mun).strip())

    identify = payload.get("identify") or {}
    for item in identify.get("fields") or []:
        name = _field_name(item)
        if name:
            cols.add(name)

    labels = payload.get("labels") or {}
    if labels.get("field"):
        cols.add(str(labels["field"]).strip())

    for item in data.get("export_columns") or []:
        if isinstance(item, str) and item.strip():
            cols.add(item.strip())

    return sorted(c for c in cols if c)


def _martin_property_type(column: str) -> str:
    if column.lower() in ("gid", "ogc_fid"):
        return "int8"
    return "string"


def sync_martin_table_properties(table: str, columns: List[str]) -> bool:
    """
    Escribe/actualiza bloque tables.{table}.properties en martin.yaml.
    Retorna True si hubo cambios (reiniciar contenedor martin).
    """
    if not yaml or not table or not columns:
        return False

    path = martin_config_path()
    if not path.is_file():
        return False

    raw_text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text) or {}
    postgres = data.setdefault("postgres", {})
    tables = postgres.setdefault("tables", {})

    props = {col: _martin_property_type(col) for col in columns}
    existing = tables.get(table)
    if isinstance(existing, dict) and existing.get("properties") == props:
        return False

    base = {
        "schema": "atlas",
        "table": table,
        "geometry_column": "the_geom",
        "srid": 3857,
        "id_column": "gid",
        "minzoom": 8,
        "maxzoom": 18,
        "clip_geom": True,
        "buffer": 64,
        "max_feature_count": 2500,
        "properties": props,
    }
    if isinstance(existing, dict):
        merged = dict(existing)
        merged["properties"] = props
        tables[table] = merged
    else:
        tables[table] = base

    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return True


def sync_martin_properties_from_payload(payload: Dict[str, Any]) -> bool:
    data = payload.get("data") or {}
    table = str(data.get("table") or "").strip()
    if not table:
        return False
    columns = collect_mvt_columns(payload)
    return sync_martin_table_properties(table, columns)
