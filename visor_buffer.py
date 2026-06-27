"""Buffer geodésico (metros) sobre geometrías GeoJSON del visor geográfico."""

import json
import logging
from typing import Any, Dict, List, Optional

from column_resolver import resolve_column
from tables import SCHEMA, T_RNC, qualified
from utils import quote_ident
from visor_export import layer_geom_column
from visor_layers import layer_config
from visor_catalog_loader import get_layer_identify_field_names

logger = logging.getLogger(__name__)

_MAX_BUFFER_M = 500_000.0
# Acapulco / Guerrero — metros en UTM 15N
_UTM_SRID = 32615


def _row_geom(row: Optional[Dict[str, Any]]) -> Optional[str]:
    if not row:
        return None
    val = row.get("geom")
    if val is None and row:
        val = next(iter(row.values()), None)
    if val is None:
        return None
    return str(val)


def _extract_geometry(geojson: Any) -> Dict[str, Any]:
    if not isinstance(geojson, dict):
        raise ValueError("GEOMETRIA_INVALIDA")
    gtype = geojson.get("type")
    if gtype == "Feature":
        geom = geojson.get("geometry")
        if not isinstance(geom, dict):
            raise ValueError("GEOMETRIA_INVALIDA")
        return geom
    if gtype in (
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
    ):
        return geojson
    if gtype == "FeatureCollection":
        feats = geojson.get("features") or []
        if not feats:
            raise ValueError("GEOMETRIA_INVALIDA")
        first = feats[0]
        if not isinstance(first, dict) or first.get("type") != "Feature":
            raise ValueError("GEOMETRIA_INVALIDA")
        geom = first.get("geometry")
        if not isinstance(geom, dict):
            raise ValueError("GEOMETRIA_INVALIDA")
        return geom
    raise ValueError("GEOMETRIA_INVALIDA")


def _layer_from_part(cfg: Dict[str, Any]) -> str:
    if cfg.get("from_sql"):
        return str(cfg["from_sql"])
    table = cfg.get("table") or ""
    if not table:
        raise ValueError("CAPA_INVALIDA")
    return qualified(table)


def _resolve_gid_column(conn, cfg: Dict[str, Any]) -> Optional[str]:
    if cfg.get("from_sql"):
        table = cfg.get("gid_table") or T_RNC
    else:
        table = cfg.get("table") or ""
    if not table:
        return None
    return resolve_column(conn, SCHEMA, table, ("gid", "GID", "ogc_fid", "OGC_FID"))


def _table_for_column_resolve(cfg: Dict[str, Any]) -> Optional[str]:
    if cfg.get("from_sql"):
        return cfg.get("gid_table") or T_RNC
    table = cfg.get("table") or ""
    return str(table).strip() or None


def _fetch_identify_properties(
    conn,
    cfg: Dict[str, Any],
    layer_id: str,
    gid: str,
    gid_col: str,
) -> Dict[str, Any]:
    """Atributos configurados en identify.fields desde PostGIS (tiles MVT suelen traer solo gid)."""
    field_names = get_layer_identify_field_names(layer_id)
    if not field_names:
        return {"gid": gid}

    table = _table_for_column_resolve(cfg)
    if not table:
        return {"gid": gid}

    select_parts: List[str] = []
    aliases: List[str] = []
    seen_cols: set[str] = set()
    for name in field_names:
        resolved = resolve_column(conn, SCHEMA, table, (name, name.upper(), name.lower()))
        if not resolved:
            continue
        col_key = resolved.lower()
        if col_key in seen_cols:
            continue
        seen_cols.add(col_key)
        alias = "gid" if col_key == gid_col.lower() else name
        aliases.append(alias)
        select_parts.append(f"TRIM({quote_ident(resolved)}::text) AS {quote_ident(alias)}")

    if not select_parts:
        return {"gid": gid}

    q_gid = quote_ident(gid_col)
    from_part = _layer_from_part(cfg)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(select_parts)}
              FROM {from_part}
             WHERE TRIM({q_gid}::text) = TRIM(%(gid)s)
             LIMIT 1
            """,
            {"gid": gid},
        )
        row = cur.fetchone()

    if not row:
        return {"gid": gid}

    props: Dict[str, Any] = {}
    for alias in aliases:
        val = row.get(alias)
        if val is None and alias != "gid":
            val = row.get(alias.lower()) or row.get(alias.upper())
        if val is not None and str(val).strip() != "":
            props[alias] = str(val).strip()
    props.setdefault("gid", gid)
    return props


def _geom4326_expr(geom_col: str) -> str:
    q = quote_ident(geom_col)
    return f"ST_Force2D(ST_Transform(ST_MakeValid({q}), 4326))"


def fetch_feature_geometry_geojson(
    conn,
    layer_id: str,
    source_gid: str,
    properties: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Geometría completa desde PostGIS (evita tiles recortados del visor)."""
    cfg = layer_config(layer_id)
    if not cfg:
        raise ValueError("CAPA_INVALIDA")

    gid_col = _resolve_gid_column(conn, cfg)
    if not gid_col:
        raise ValueError("GID_NO_DISPONIBLE")

    gid = str(source_gid or "").strip()
    if not gid:
        raise ValueError("GID_NO_DISPONIBLE")

    geom_col = layer_geom_column(conn, cfg)
    q_gid = quote_ident(gid_col)
    geom_expr = _geom4326_expr(geom_col)
    from_part = _layer_from_part(cfg)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ST_AsGeoJSON({geom_expr}) AS geom
              FROM {from_part}
             WHERE TRIM({q_gid}::text) = TRIM(%(gid)s)
               AND {quote_ident(geom_col)} IS NOT NULL
             LIMIT 1
            """,
            {"gid": gid},
        )
        row = cur.fetchone()

    geom_json = _row_geom(row)
    if not geom_json:
        raise ValueError("ELEMENTO_NO_ENCONTRADO")

    geometry = json.loads(geom_json)
    props = _fetch_identify_properties(conn, cfg, layer_id, gid, gid_col)
    if properties:
        props = {**props, **properties}
    props.setdefault("gid", gid)
    return {"type": "Feature", "properties": props, "geometry": geometry}


def _outline4326_expr(geom_expr: str) -> str:
    """Contorno fiel al polígono: anillos exteriores sin simplificar (evita esquinas artificiales)."""
    g = f"ST_MakeValid({geom_expr})"
    u = f"ST_Transform({g}, {_UTM_SRID})"
    polys = f"ST_CollectionExtract({u}, 3)"
    rings = (
        f"(SELECT ST_Collect(ST_ExteriorRing((d).geom)) "
        f"FROM ST_Dump({polys}) AS d "
        f"WHERE ST_GeometryType((d).geom) = 'ST_Polygon')"
    )
    return f"ST_Transform({rings}, 4326)"


def fetch_feature_outline_geojson(
    conn,
    layer_id: str,
    source_gid: str,
    properties: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Contorno del polígono desde PostGIS (LineString/MultiLineString)."""
    cfg = layer_config(layer_id)
    if not cfg:
        raise ValueError("CAPA_INVALIDA")
    if cfg.get("geom_type") not in ("polygon",):
        raise ValueError("GEOMETRIA_INVALIDA")

    gid_col = _resolve_gid_column(conn, cfg)
    if not gid_col:
        raise ValueError("GID_NO_DISPONIBLE")

    gid = str(source_gid or "").strip()
    if not gid:
        raise ValueError("GID_NO_DISPONIBLE")

    geom_col = layer_geom_column(conn, cfg)
    q_gid = quote_ident(gid_col)
    geom_expr = _geom4326_expr(geom_col)
    outline_expr = _outline4326_expr(geom_expr)
    from_part = _layer_from_part(cfg)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ST_AsGeoJSON({outline_expr}) AS geom
              FROM {from_part}
             WHERE TRIM({q_gid}::text) = TRIM(%(gid)s)
               AND {quote_ident(geom_col)} IS NOT NULL
             LIMIT 1
            """,
            {"gid": gid},
        )
        row = cur.fetchone()

    geom_json = _row_geom(row)
    if not geom_json:
        raise ValueError("ELEMENTO_NO_ENCONTRADO")

    geometry = json.loads(geom_json)
    props: Dict[str, Any] = dict(properties or {})
    props.setdefault("gid", gid)
    props["atlasPickOutline"] = True
    return {"type": "Feature", "properties": props, "geometry": geometry}


def _normalize_line_side(line_side: Optional[str]) -> Optional[str]:
    if line_side is None:
        return None
    side = str(line_side).strip().lower()
    if side in ("", "both"):
        return None
    if side in ("left", "right"):
        return side
    raise ValueError("LADO_INVALIDO")


def _line_utm_expr(geom_expr: str) -> str:
    return (
        f"ST_SimplifyPreserveTopology("
        f"ST_Transform(ST_LineMerge(ST_MakeValid({geom_expr})), {_UTM_SRID}), "
        f"GREATEST(%(dist)s * 0.02, 0.5)"
        f")"
    )


def _buffer_corridor_sql(geom_expr: str) -> str:
    return (
        f"ST_AsGeoJSON(ST_MakeValid("
        f"ST_Buffer({geom_expr}::geography, %(dist)s)::geometry"
        f"))"
    )


def _buffer_side_strategy_exprs(geom_expr: str, side: str) -> List[str]:
    """Varias estrategias PostGIS para inundación lateral (líneas complejas)."""
    u = _line_utm_expr(geom_expr)
    exprs: List[str] = []

    if side == "left":
        exprs.append(
            f"ST_AsGeoJSON(ST_Transform(ST_MakeValid("
            f"ST_Buffer({u}, %(dist)s, 'endcap=flat join=round single_sided=true')"
            f"), 4326))"
        )
    else:
        exprs.append(
            f"ST_AsGeoJSON(ST_Transform(ST_MakeValid("
            f"ST_Reverse(ST_Buffer(ST_Reverse({u}), %(dist)s, "
            f"'endcap=flat join=round single_sided=true'))"
            f"), 4326))"
        )

    exprs.append(
        f"ST_AsGeoJSON(ST_Transform(ST_MakeValid("
        f"ST_MakePolygon("
        f"ST_AddPoint("
        f"ST_LineConcat({u}, ST_Reverse(ST_OffsetCurve({u}, %(signed_dist)s, "
        f"'join=round endcap=flat'))), "
        f"ST_StartPoint({u})"
        f"))), 4326))"
    )

    if side == "left":
        geog = (
            f"ST_Buffer({geom_expr}::geography, %(dist)s, "
            f"'endcap=flat join=round single_sided=true')::geometry"
        )
    else:
        geog = (
            f"ST_Reverse("
            f"ST_Buffer(ST_Reverse({geom_expr})::geography, %(dist)s, "
            f"'endcap=flat join=round single_sided=true')::geometry"
            f")"
        )
    exprs.append(f"ST_AsGeoJSON(ST_MakeValid({geog}))")

    return exprs


def _buffer_side_params(dist: float, side: str) -> Dict[str, Any]:
    signed = float(dist) if side == "left" else -float(dist)
    return {"dist": dist, "signed_dist": signed}


def _run_buffer_select(
    conn,
    select_expr: str,
    from_part: str,
    where_clause: str,
    params: Dict[str, Any],
) -> Optional[str]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {select_expr} AS geom FROM {from_part} WHERE {where_clause} LIMIT 1",
                params,
            )
            row = cur.fetchone()
        return _row_geom(row)
    except Exception as exc:
        logger.warning("visor buffer strategy failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def _buffer_corridor_strategies(geom_expr: str) -> List[str]:
    """Corredor (ambos lados): geography y respaldo UTM."""
    u = _line_utm_expr(geom_expr)
    return [
        _buffer_corridor_sql(geom_expr),
        (
            f"ST_AsGeoJSON(ST_Transform(ST_MakeValid("
            f"ST_Buffer({u}, %(dist)s, 'endcap=round join=round')"
            f"), 4326))"
        ),
    ]


def _buffer_full_geom(
    conn,
    geom_expr: str,
    from_part: str,
    where_clause: str,
    dist: float,
    line_side: Optional[str],
    extra_params: Optional[Dict[str, Any]] = None,
) -> str:
    side = _normalize_line_side(line_side)
    base_params = dict(extra_params or {})
    base_params["dist"] = dist

    if side:
        params = {**_buffer_side_params(dist, side), **base_params}
        for expr in _buffer_side_strategy_exprs(geom_expr, side):
            geom_json = _run_buffer_select(conn, expr, from_part, where_clause, params)
            if geom_json:
                return geom_json
        raise ValueError("BUFFER_FALLIDO")

    for expr in _buffer_corridor_strategies(geom_expr):
        geom_json = _run_buffer_select(conn, expr, from_part, where_clause, base_params)
        if geom_json:
            return geom_json
    raise ValueError("BUFFER_FALLIDO")


def _build_buffer_feature(
    buffered_geom: Dict[str, Any],
    dist: float,
    props_base: Optional[Dict[str, Any]] = None,
    *,
    gid: Optional[str] = None,
    layer_id: Optional[str] = None,
    side: Optional[str] = None,
) -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "atlasBuffer": True,
        "atlasBufferSource": "map_pick",
        "atlasBufferDistanceM": dist,
    }
    if gid:
        props["atlasBufferSourceGid"] = gid
    if layer_id:
        props["atlasBufferLayerId"] = layer_id
    if side:
        props["atlasBufferSide"] = side
    if props_base:
        for key in ("nombre", "NOMBRE", "nom_asen", "nom_loc", "gid"):
            if props_base.get(key) is not None:
                props.setdefault(key, props_base.get(key))
    return {"type": "Feature", "properties": props, "geometry": buffered_geom}


def buffer_from_layer_gid(
    conn,
    layer_id: str,
    source_gid: str,
    distance_m: float,
    properties: Optional[Dict[str, Any]] = None,
    line_side: Optional[str] = None,
) -> Dict[str, Any]:
    """Buffer sobre geometría completa en PostGIS (por gid de capa del visor)."""
    try:
        dist = float(distance_m)
    except (TypeError, ValueError) as exc:
        raise ValueError("DISTANCIA_INVALIDA") from exc
    if dist <= 0 or dist > _MAX_BUFFER_M:
        raise ValueError("DISTANCIA_INVALIDA")

    cfg = layer_config(layer_id)
    if not cfg:
        raise ValueError("CAPA_INVALIDA")

    gid_col = _resolve_gid_column(conn, cfg)
    if not gid_col:
        raise ValueError("GID_NO_DISPONIBLE")

    gid = str(source_gid or "").strip()
    if not gid:
        raise ValueError("GID_NO_DISPONIBLE")

    geom_col = layer_geom_column(conn, cfg)
    q_gid = quote_ident(gid_col)
    geom_expr = _geom4326_expr(geom_col)
    from_part = _layer_from_part(cfg)
    side = _normalize_line_side(line_side)
    if side and cfg.get("geom_type") not in ("line",):
        side = None

    where = (
        f"TRIM({q_gid}::text) = TRIM(%(gid)s) AND {quote_ident(geom_col)} IS NOT NULL"
    )
    geom_json = _buffer_full_geom(
        conn, geom_expr, from_part, where, dist, side, extra_params={"gid": gid}
    )
    if not geom_json:
        raise ValueError("ELEMENTO_NO_ENCONTRADO")

    buffered_geom = json.loads(geom_json)
    return _build_buffer_feature(
        buffered_geom,
        dist,
        properties,
        gid=gid,
        layer_id=layer_id,
        side=side,
    )


def buffer_geometry_geojson(
    conn,
    geojson: Any,
    distance_m: float,
    layer_id: Optional[str] = None,
    source_gid: Optional[str] = None,
    line_side: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ST_Buffer en geography (metros).
    Si hay layer_id + source_gid, usa la geometría completa en PostGIS (recomendado).
    """
    props = {}
    if isinstance(geojson, dict) and geojson.get("type") == "Feature":
        src_props = geojson.get("properties") or {}
        if isinstance(src_props, dict):
            props = dict(src_props)

    gid = str(source_gid or props.get("gid") or props.get("GID") or "").strip()
    layer = str(layer_id or "").strip().lower()

    if layer and gid:
        return buffer_from_layer_gid(conn, layer, gid, distance_m, props, line_side=line_side)

    try:
        dist = float(distance_m)
    except (TypeError, ValueError) as exc:
        raise ValueError("DISTANCIA_INVALIDA") from exc
    if dist <= 0 or dist > _MAX_BUFFER_M:
        raise ValueError("DISTANCIA_INVALIDA")

    geom = _extract_geometry(geojson)
    side = _normalize_line_side(line_side)
    gtype = geom.get("type")
    if side and gtype not in ("LineString", "MultiLineString"):
        side = None

    geom_expr = "ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s), 4326)"
    geom_json = json.dumps(geom, ensure_ascii=False)

    if side:
        exprs = _buffer_side_strategy_exprs(geom_expr, side)
        params = {**_buffer_side_params(dist, side), "geom": geom_json}
        geom_out = None
        for expr in exprs:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {expr} AS geom", params)
                    row = cur.fetchone()
                geom_out = _row_geom(row)
                if geom_out:
                    break
            except Exception as exc:
                logger.warning("visor buffer geojson side strategy failed: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
        if not geom_out:
            raise ValueError("BUFFER_FALLIDO")
    else:
        geom_out = None
        params = {"geom": geom_json, "dist": dist}
        for expr in _buffer_corridor_strategies(geom_expr):
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {expr} AS geom", params)
                    row = cur.fetchone()
                geom_out = _row_geom(row)
                if geom_out:
                    break
            except Exception as exc:
                logger.warning("visor buffer geojson corridor strategy failed: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
        if not geom_out:
            raise ValueError("BUFFER_FALLIDO")

    buffered_geom = json.loads(geom_out)
    out_gid = gid or (str(props.get("gid")).strip() if props.get("gid") is not None else None)
    return _build_buffer_feature(buffered_geom, dist, props, gid=out_gid or None, side=side)
