"""Índice de búsqueda del visor geográfico a partir de catalog.json."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence

from tables import qualified
from utils import mun_where_sql
from visor_catalog_loader import load_visor_catalog_raw

_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _safe_ident(name: str, *, kind: str = "column") -> str:
    raw = (name or "").strip()
    if not raw or not _IDENT_RE.match(raw):
        raise ValueError(f"{kind} inválido: {name!r}")
    return f'"{raw.replace(chr(34), chr(34) + chr(34))}"'


def _safe_table(name: str) -> str:
    raw = (name or "").strip().lower()
    if not raw or not _TABLE_RE.match(raw):
        raise ValueError(f"tabla inválida: {name!r}")
    return raw


def _codigo_act_predicate(codigos: Sequence[int]) -> str:
    codes = ", ".join(f"'{int(c)}'" for c in codigos)
    return f"regexp_replace(TRIM(codigo_act::text), '[^0-9]', '', 'g') IN ({codes})"


def _normalize_scope(raw: Any) -> str:
    val = str(raw or "both").strip().lower()
    if val in ("estatal", "estatal_only", "state"):
        return "estatal"
    if val in ("municipio", "municipio_only", "mun"):
        return "municipio"
    return "both"


def _normalize_geom_mode(raw: Any, geometry: str = "") -> str:
    val = str(raw or "").strip().lower()
    if val in ("point", "centroid", "polygon"):
        return val
    geom = str(geometry or "").strip().lower()
    if geom == "point":
        return "point"
    if geom in ("polygon", "line"):
        return "centroid"
    return "point"


def _entry_from_layer(layer_id: str, layer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    search = layer.get("search")
    if not isinstance(search, dict) or not search.get("enabled"):
        return None

    data = layer.get("data") or {}
    table = data.get("table") or data.get("gid_table")
    if not table:
        return None

    name_column = search.get("name_column")
    if not name_column:
        return None

    search_columns = search.get("search_columns")
    if not search_columns:
        search_columns = [name_column]
    if not isinstance(search_columns, list) or not search_columns:
        return None

    id_column = search.get("id_column") or "cvegeo"
    filt = data.get("filter") or {}
    codigos = filt.get("codigo_act")

    mun_filter = search.get("mun_filter")
    if mun_filter is None:
        mun_filter = bool(data.get("mun_filter")) or data.get("mun_filter_cvegeo") is not False

    mun_filter_cvegeo = search.get("mun_filter_cvegeo")
    if mun_filter_cvegeo is None:
        mun_filter_cvegeo = data.get("mun_filter_cvegeo") is not False

    return {
        "layer_id": layer_id,
        "table": _safe_table(str(table)),
        "tipo": str(search.get("tipo") or layer.get("label") or layer_id),
        "name_column": str(name_column),
        "search_columns": [str(c) for c in search_columns],
        "id_column": str(id_column),
        "geom_mode": _normalize_geom_mode(search.get("geom_mode"), layer.get("geometry") or ""),
        "scope": _normalize_scope(search.get("scope")),
        "mun_filter": bool(mun_filter),
        "mun_filter_cvegeo": bool(mun_filter_cvegeo),
        "highlight": search.get("highlight", True),
        "codigo_act": tuple(int(c) for c in codigos) if codigos else None,
    }


def _entry_from_extra(extra: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(extra, dict) or not extra.get("enabled", True):
        return None
    table = extra.get("table")
    name_column = extra.get("name_column")
    if not table or not name_column:
        return None
    search_columns = extra.get("search_columns") or [name_column]
    return {
        "layer_id": str(extra.get("id") or extra.get("table")),
        "table": _safe_table(str(table)),
        "tipo": str(extra.get("tipo") or extra.get("label") or table),
        "name_column": str(name_column),
        "search_columns": [str(c) for c in search_columns],
        "id_column": str(extra.get("id_column") or "cvegeo"),
        "geom_mode": _normalize_geom_mode(extra.get("geom_mode"), extra.get("geometry") or "polygon"),
        "scope": _normalize_scope(extra.get("scope", "estatal")),
        "mun_filter": bool(extra.get("mun_filter", False)),
        "mun_filter_cvegeo": bool(extra.get("mun_filter_cvegeo", True)),
        "highlight": extra.get("highlight", True),
        "codigo_act": None,
    }


@lru_cache
def search_index_from_catalog() -> List[Dict[str, Any]]:
    raw = load_visor_catalog_raw()
    entries: List[Dict[str, Any]] = []

    for extra in raw.get("search_extras") or []:
        if not isinstance(extra, dict):
            continue
        item = _entry_from_extra(extra)
        if item:
            entries.append(item)

    layers = raw.get("layers") or {}
    ordered_ids: List[str] = []
    for group in raw.get("groups") or []:
        for lid in group.get("layers") or []:
            if lid not in ordered_ids:
                ordered_ids.append(lid)
    for lid in layers:
        if lid not in ordered_ids:
            ordered_ids.append(lid)

    for lid in ordered_ids:
        layer = layers.get(lid)
        if not isinstance(layer, dict):
            continue
        item = _entry_from_layer(lid, layer)
        if item:
            entries.append(item)

    return entries


def clear_search_catalog_cache() -> None:
    """Invalida caché tras editar catalog.json (útil en desarrollo)."""
    search_index_from_catalog.cache_clear()
    from visor_catalog_loader import load_visor_catalog_raw

    load_visor_catalog_raw.cache_clear()


def search_limit_per_source() -> int:
    raw = load_visor_catalog_raw()
    search_cfg = raw.get("search") or {}
    try:
        lim = int(search_cfg.get("limit_per_source", 5))
    except (TypeError, ValueError):
        lim = 5
    return max(1, min(lim, 20))


def search_config_for_api() -> Dict[str, Any]:
    sources = []
    for entry in search_index_from_catalog():
        sources.append(
            {
                "layer_id": entry["layer_id"],
                "table": entry["table"],
                "tipo": entry["tipo"],
                "scope": entry["scope"],
                "geom_mode": entry["geom_mode"],
            }
        )
    return {
        "limit_per_source": search_limit_per_source(),
        "sources": sources,
    }


def geom_lookup_for_table(tabla: str) -> Optional[Dict[str, Any]]:
    tabla_lc = (tabla or "").strip().lower()
    for entry in search_index_from_catalog():
        if entry["table"] == tabla_lc:
            return {
                "table": entry["table"],
                "id_column": entry["id_column"],
                "geom_mode": entry["geom_mode"],
                "highlight": entry.get("highlight", True),
            }
    return None


def wgs84_coords_sql(geom_mode: str) -> str:
    if geom_mode == "point":
        return """
        ST_X(ST_Transform(the_geom, 4326)) AS lng,
        ST_Y(ST_Transform(the_geom, 4326)) AS lat"""
    return """
        ST_X(ST_Transform(ST_Centroid(the_geom), 4326)) AS lng,
        ST_Y(ST_Transform(ST_Centroid(the_geom), 4326)) AS lat"""


def build_search_sql(scoped: bool, limit_per_source: int) -> str:
    """Construye UNION ALL parametrizado para /api/buscar (compatibilidad)."""
    parts = [
        build_search_sql_for_entry(entry, scoped, limit_per_source)
        for entry in search_index_from_catalog()
        if _entry_in_scope(entry, scoped)
    ]
    if not parts:
        return "SELECT NULL::text AS nombre_busqueda WHERE false"
    return "\nUNION ALL\n".join(parts)


def _entry_in_scope(entry: Dict[str, Any], scoped: bool) -> bool:
    scope = entry["scope"]
    if scoped and scope == "estatal":
        return False
    if not scoped and scope == "municipio":
        return False
    return True


def build_search_sql_for_entry(
    entry: Dict[str, Any], scoped: bool, limit_per_source: int
) -> str:
    """Subconsulta UNION ALL para una sola fuente del catálogo."""
    lim = max(1, min(int(limit_per_source), 20))

    q_table = qualified(entry["table"])
    name_col = _safe_ident(entry["name_column"])
    id_col = _safe_ident(entry["id_column"])
    coords = wgs84_coords_sql(entry["geom_mode"])
    tipo = entry["tipo"].replace("'", "''")

    search_conds = [
        f"TRIM(BOTH FROM {_safe_ident(col)}::text) ILIKE %(query)s"
        for col in entry["search_columns"]
    ]
    where_parts = [f"({' OR '.join(search_conds)})"]

    if scoped and entry["mun_filter"]:
        where_parts.append(
            mun_where_sql("", with_cvegeo=entry.get("mun_filter_cvegeo", True))
        )

    if entry.get("codigo_act"):
        where_parts.append(_codigo_act_predicate(entry["codigo_act"]))

    where_sql = " AND ".join(where_parts)
    return f"""(
    SELECT
        TRIM(BOTH FROM {name_col}::text) AS nombre_busqueda,
        '{tipo}' AS tipo,
        '{entry["table"]}' AS tabla_origen,
        TRIM(BOTH FROM {id_col}::text) AS id_origen,
        '{entry["geom_mode"]}' AS geom_tipo,
        {coords}
    FROM {q_table}
    WHERE {where_sql}
      AND the_geom IS NOT NULL
    LIMIT {lim}
)"""
