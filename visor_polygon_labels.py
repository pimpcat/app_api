"""Puntos de etiqueta para polígonos del visor (una etiqueta por feature, sin duplicados MVT)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from database import get_db
from tables import qualified
from utils import is_mun_cve3, mun_where_sql, norm_cve_mun, quote_ident
from visor_attribute_filter import attribute_filter_where_sql, parse_attribute_filter
from visor_catalog_loader import load_visor_catalog_raw

_IDENT_RE = __import__("re").compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _safe_column(name: str) -> str:
    raw = (name or "").strip()
    if not raw or not _IDENT_RE.match(raw):
        raise ValueError(f"COLUMN_INVALID:{raw}")
    return quote_ident(raw)


def _label_source(entry: Dict[str, Any]) -> str:
    labels = entry.get("labels") or {}
    source = str(labels.get("source") or "").strip().lower()
    if source in ("vector", "mvt", "tile"):
        return "vector"
    if source == "centroid":
        return "centroid"
    geometry = str(entry.get("geometry") or "").strip().lower()
    return "centroid" if geometry == "polygon" else "vector"


def polygon_label_geojson(
    layer_id: str,
    cve_mun: Optional[str] = None,
    *,
    state_wide: bool = False,
) -> Dict[str, Any]:
    """
    FeatureCollection de puntos (ST_PointOnSurface) para etiquetar polígonos sin
    duplicar texto por tesela vectorial.
    """
    lid = (layer_id or "").strip().lower()
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    entry = layers.get(lid)
    if not isinstance(entry, dict):
        raise ValueError("LAYER_NOT_FOUND")

    labels = entry.get("labels") or {}
    if labels.get("enabled") is False:
        return _empty_fc(lid, cve_mun, state_wide)

    if _label_source(entry) != "centroid":
        raise ValueError("NOT_CENTROID_LABELS")

    geometry = str(entry.get("geometry") or "").strip().lower()
    if geometry != "polygon":
        raise ValueError("NOT_POLYGON_LAYER")

    field = str(labels.get("field") or "").strip()
    if not field:
        raise ValueError("LABEL_FIELD_REQUIRED")

    data = entry.get("data") or {}
    table = str(data.get("table") or "").strip().lower()
    if not table:
        raise ValueError("TABLE_REQUIRED")

    search = entry.get("search") or {}
    id_col = str(labels.get("id_column") or search.get("id_column") or "gid").strip()

    cve = norm_cve_mun(cve_mun or "")
    mun_f = data.get("mun_filter")
    scoped = (
        not state_wide
        and bool(cve and is_mun_cve3(cve))
        and mun_f is not False
    )

    q_table = qualified(table)
    q_field = _safe_column(field)
    q_id = _safe_column(id_col)
    q_geom = quote_ident("the_geom")

    where_parts = [
        f"{q_geom} IS NOT NULL",
        f"TRIM(COALESCE({q_field}::text, '')) <> ''",
    ]
    params: Dict[str, Any] = {}
    if scoped:
        where_parts.append(mun_where_sql("", with_cvegeo=True))
        params["cve"] = cve

    attr_f = parse_attribute_filter(data)
    if attr_f:
        attr_sql = attribute_filter_where_sql(attr_f)
        if attr_sql:
            where_parts.append(attr_sql)

    where_sql = " AND ".join(where_parts)
    sql = f"""
        SELECT TRIM({q_id}::text) AS feat_id,
               TRIM({q_field}::text) AS label_text,
               ST_X(ST_Transform(ST_PointOnSurface({q_geom}), 4326)) AS lon,
               ST_Y(ST_Transform(ST_PointOnSurface({q_geom}), 4326)) AS lat
          FROM {q_table}
         WHERE {where_sql}
    """

    features: List[Dict[str, Any]] = []
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or None)
            rows = cur.fetchall()

    field_upper = field.upper()
    for row in rows:
        lon, lat = row.get("lon"), row.get("lat")
        text = (row.get("label_text") or "").strip()
        if lon is None or lat is None or not text:
            continue
        props: Dict[str, Any] = {field: text, field_upper: text}
        feat_id = row.get("feat_id")
        if feat_id is not None and str(feat_id).strip():
            props[id_col] = str(feat_id).strip()
            props[id_col.upper()] = str(feat_id).strip()
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                "properties": props,
            }
        )

    return {
        "ok": True,
        "layer_id": lid,
        "cve_mun": cve if scoped else None,
        "state_wide": state_wide,
        "count": len(features),
        "featureCollection": {"type": "FeatureCollection", "features": features},
    }


def _empty_fc(layer_id: str, cve_mun: Optional[str], state_wide: bool) -> Dict[str, Any]:
    cve = norm_cve_mun(cve_mun or "")
    return {
        "ok": True,
        "layer_id": layer_id,
        "cve_mun": cve if cve and not state_wide else None,
        "state_wide": state_wide,
        "count": 0,
        "featureCollection": {"type": "FeatureCollection", "features": []},
    }
