"""
Puntos GeoJSON para capas del visor con agrupación (clusters) en MapLibre.

MapLibre solo agrupa fuentes GeoJSON en el cliente; esta consulta devuelve
los puntos del municipio activo (cve_mun) o de todo el estado (scope estatal)
para alimentar esa fuente.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

from column_resolver import resolve_column
from tables import SCHEMA, qualified
from utils import mun_where_sql, norm_cve_mun, quote_ident
from visor_export import (
    MAX_FEATURES,
    _append_where_parts,
    layer_attribute_columns,
    layer_geom_column,
    layer_uses_cvegeo_filter,
    layer_uses_mun_filter,
)
from visor_layers import layer_config

logger = logging.getLogger(__name__)

CLUSTER_ERRORS = {
    "UNKNOWN_LAYER": "Capa no disponible.",
    "MISSING_CVE_MUN": "Selecciona un municipio en el explorador.",
    "NOT_POINT_LAYER": "La agrupación solo aplica a capas de punto.",
    "CLUSTER_DISABLED": "Esta capa no tiene clusters habilitados.",
    "NO_FEATURES": "No hay puntos en el municipio seleccionado.",
    "TOO_MANY": "Demasiados puntos para agrupar en el mapa.",
}


def cluster_error_message(code: str) -> str:
    return CLUSTER_ERRORS.get(code, code)


def _cluster_enabled(layer_id: str, cfg: Dict[str, Any]) -> bool:
    style = cfg.get("style") or {}
    cluster = style.get("cluster")
    if isinstance(cluster, dict) and cluster.get("enabled"):
        return True
    from visor_catalog_loader import load_visor_catalog_raw

    layer_key = (layer_id or "").strip().lower()
    entry = (load_visor_catalog_raw().get("layers") or {}).get(layer_key) or {}
    raw_cluster = (entry.get("style") or {}).get("cluster")
    return isinstance(raw_cluster, dict) and bool(raw_cluster.get("enabled"))


def _point_geometry(geom: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(geom, dict):
        return None
    gtype = geom.get("type")
    if gtype == "Point":
        return geom
    if gtype == "MultiPoint":
        coords = geom.get("coordinates") or []
        if coords and isinstance(coords[0], (list, tuple)) and len(coords[0]) >= 2:
            return {"type": "Point", "coordinates": coords[0]}
    return None


def _label_columns_for_layer(layer_key: str) -> List[str]:
    """Columnas de texto necesarias para etiquetas del catálogo (GeoJSON cluster)."""
    from visor_catalog_loader import load_visor_catalog_raw

    entry = (load_visor_catalog_raw().get("layers") or {}).get(layer_key) or {}
    labels = entry.get("labels") or {}
    if labels.get("enabled") is False:
        return []

    cols: List[str] = []

    def add(col: Any) -> None:
        if col is None:
            return
        name = str(col).strip()
        if name and name not in cols:
            cols.append(name)

    field = labels.get("field")
    if field:
        add(field)
    for item in labels.get("fields") or []:
        add(item if isinstance(item, str) else (item or {}).get("column"))
    join = labels.get("join") or {}
    for side in ("left", "right"):
        for item in join.get(side) or []:
            add(item)

    return cols


def _identify_columns_for_layer(layer_key: str) -> List[str]:
    """Columnas de identify.fields del catálogo (GeoJSON cluster + identify)."""
    from visor_catalog_loader import load_visor_catalog_raw

    entry = (load_visor_catalog_raw().get("layers") or {}).get(layer_key) or {}
    identify = entry.get("identify") or {}
    cols: List[str] = []

    def add(col: Any) -> None:
        if col is None:
            return
        name = str(col).strip()
        if name and name not in cols:
            cols.append(name)

    for item in identify.get("fields") or []:
        if isinstance(item, str):
            add(item)
        elif isinstance(item, dict):
            add(item.get("column") or item.get("field") or item.get("name"))
    join = identify.get("join") or {}
    for side in ("left", "right"):
        for item in join.get(side) or []:
            add(item)

    return cols


def _cluster_geojson_columns(conn, layer_key: str, cfg: Dict[str, Any]) -> List[str]:
    cols = layer_attribute_columns(conn, cfg, "kml")
    seen = {c.lower() for c in cols}
    for col in _label_columns_for_layer(layer_key):
        if col.lower() not in seen:
            cols.append(col)
            seen.add(col.lower())
    for col in _identify_columns_for_layer(layer_key):
        if col.lower() not in seen:
            cols.append(col)
            seen.add(col.lower())
    return cols


def _table_for_cluster_resolve(cfg: Dict[str, Any]) -> Optional[str]:
    if cfg.get("from_sql"):
        t = cfg.get("gid_table") or cfg.get("table")
        return str(t).strip() or None
    t = cfg.get("table")
    return str(t).strip() or None


def _resolve_cluster_prop_columns(
    conn,
    layer_key: str,
    cfg: Dict[str, Any],
) -> List[tuple[str, str]]:
    """Pares (alias catálogo, columna física) para props del GeoJSON cluster."""
    table = _table_for_cluster_resolve(cfg)
    if not table:
        return []

    catalog_names = _cluster_geojson_columns(conn, layer_key, cfg)
    pairs: List[tuple[str, str]] = []
    seen: set[str] = set()

    gid_col = resolve_column(conn, SCHEMA, table, ("gid", "GID", "ogc_fid", "OGC_FID"))
    if gid_col:
        pairs.append(("gid", gid_col))
        seen.add("gid")

    for name in catalog_names:
        lc = name.lower()
        if lc in seen:
            continue
        resolved = resolve_column(conn, SCHEMA, table, (name, name.upper(), name.lower()))
        if resolved:
            pairs.append((name, resolved))
            seen.add(lc)
    return pairs


def _geom_json_expr(geom_col: str) -> str:
    q = quote_ident(geom_col)
    return f"ST_AsGeoJSON(ST_Force2D(ST_Transform({q}, 4326)), 6)::text AS geom_json"


def _build_cluster_select_sql(
    cfg: Dict[str, Any],
    prop_pairs: Sequence[tuple[str, str]],
    with_cvegeo: bool,
    geom_col: str,
    apply_mun_filter: bool,
) -> str:
    if not prop_pairs:
        raise ValueError("NO_FEATURES")

    select_parts = [
        f"TRIM({quote_ident(col)}::text) AS {quote_ident(alias)}"
        for alias, col in prop_pairs
    ]
    select_parts.append(_geom_json_expr(geom_col))

    q_geom = quote_ident(geom_col)
    where_parts = [f"{q_geom} IS NOT NULL"]
    if apply_mun_filter:
        where_parts.append(mun_where_sql("", with_cvegeo))
    _append_where_parts(cfg, where_parts)
    where = " AND ".join(where_parts)

    from_part = cfg["from_sql"] if cfg.get("from_sql") else qualified(cfg["table"])
    return (
        f"SELECT {', '.join(select_parts)} FROM {from_part}"
        f" WHERE {where} LIMIT {MAX_FEATURES}"
    )


def fetch_layer_points_geojson(
    conn,
    layer_id: str,
    cve_mun: str,
    *,
    state_wide: bool = False,
    max_features: int = MAX_FEATURES,
) -> Dict[str, Any]:
    """FeatureCollection EPSG:4326 de puntos (municipio o alcance estatal del visor)."""
    layer_key = (layer_id or "").strip().lower()
    cfg = layer_config(layer_key)
    if not cfg:
        raise ValueError("UNKNOWN_LAYER")
    if str(cfg.get("geom_type") or "").lower() != "point":
        raise ValueError("NOT_POINT_LAYER")
    if not _cluster_enabled(layer_key, cfg):
        raise ValueError("CLUSTER_DISABLED")

    apply_mun_filter = layer_uses_mun_filter(cfg) and not state_wide
    cve = norm_cve_mun(cve_mun)
    if apply_mun_filter and not cve:
        raise ValueError("MISSING_CVE_MUN")
    if not apply_mun_filter:
        cve = norm_cve_mun(cve_mun) or "estatal"

    geom_col = layer_geom_column(conn, cfg)
    prop_pairs = _resolve_cluster_prop_columns(conn, layer_key, cfg)
    if not prop_pairs:
        raise ValueError("NO_FEATURES")

    prop_aliases = [alias for alias, _ in prop_pairs]
    with_cvegeo = layer_uses_cvegeo_filter(conn, cfg) if apply_mun_filter else False
    sql = _build_cluster_select_sql(cfg, prop_pairs, with_cvegeo, geom_col, apply_mun_filter)
    sql_params = {"cve": cve} if apply_mun_filter else {}

    with conn.cursor() as cur:
        cur.execute("SET statement_timeout TO 120000")
        cur.execute(sql, sql_params)
        rows = cur.fetchall()

    features: List[Dict[str, Any]] = []
    for row in rows:
        raw = row.get("geom_json") if isinstance(row, dict) else None
        if not raw:
            continue
        try:
            geom = json.loads(raw)
        except json.JSONDecodeError:
            continue
        point = _point_geometry(geom)
        if not point:
            continue
        props: Dict[str, Any] = {}
        if isinstance(row, dict):
            for alias in prop_aliases:
                val = row.get(alias)
                if val is None:
                    val = row.get(alias.lower()) or row.get(alias.upper())
                if val is not None and str(val).strip() != "":
                    props[alias] = val
        features.append({"type": "Feature", "geometry": point, "properties": props})
        if len(features) >= max_features:
            break

    if not features:
        raise ValueError("NO_FEATURES")
    if len(features) >= max_features:
        logger.warning("cluster geojson truncated at %s for layer %s", max_features, layer_key)

    return {"type": "FeatureCollection", "features": features}
