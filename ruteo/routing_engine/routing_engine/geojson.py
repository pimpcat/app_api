"""
Construcción de GeoJSON para rutas y puntos origen/destino.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from utils import quote_ident


def normalize_route_geometry(geom: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normaliza geometría de ruta a LineString o MultiLineString en WGS84.

    Convierte GeometryCollection (p. ej. por mezcla LineString/MultiLineString
    en la red) a un tipo dibujable en MapLibre.
    """
    if not geom:
        return {"type": "LineString", "coordinates": []}

    gtype = geom.get("type")
    coords = geom.get("coordinates")

    if gtype == "LineString" and isinstance(coords, list):
        if len(coords) >= 2:
            return geom
        if len(coords) == 1 and isinstance(coords[0], list):
            return {"type": "LineString", "coordinates": [coords[0], coords[0]]}
        return {"type": "LineString", "coordinates": []}

    if gtype == "MultiLineString" and isinstance(coords, list):
        valid = [ln for ln in coords if isinstance(ln, list) and len(ln) >= 2]
        if len(valid) == 1:
            return {"type": "LineString", "coordinates": valid[0]}
        if valid:
            return {"type": "MultiLineString", "coordinates": valid}
        return {"type": "LineString", "coordinates": []}

    if gtype == "GeometryCollection":
        lines: List[List[Any]] = []
        for part in geom.get("geometries") or []:
            if not isinstance(part, dict):
                continue
            pt = part.get("type")
            pc = part.get("coordinates")
            if pt == "LineString" and isinstance(pc, list) and len(pc) >= 2:
                lines.append(pc)
            elif pt == "MultiLineString" and isinstance(pc, list):
                for ln in pc:
                    if isinstance(ln, list) and len(ln) >= 2:
                        lines.append(ln)
        if len(lines) == 1:
            return {"type": "LineString", "coordinates": lines[0]}
        if lines:
            return {"type": "MultiLineString", "coordinates": lines}
        return {"type": "LineString", "coordinates": []}

    return geom


def build_route_geom_json(
    conn,
    route_meta: Dict[str, Any],
    path_rows: List[Dict[str, Any]],
) -> Optional[str]:
    """
    Geometría de la ruta: ST_LineMerge por tramo + ST_Collect ordenado.

    Evita GeometryCollection vacía al mezclar LineString y MultiLineString.
    """
    edge_ids = [int(r["edge"]) for r in path_rows]
    path_seqs = [int(r["path_seq"]) for r in path_rows]
    if not edge_ids:
        return None

    eid = quote_ident(route_meta.get("edge_id") or "gid")
    geom = quote_ident(route_meta.get("geom") or "the_geom")
    tbl = route_meta["table"]

    sql = f"""
        WITH ordered AS (
            SELECT u.path_seq,
                   ST_LineMerge(r.{geom}) AS geom
              FROM unnest(%(edge_ids)s::bigint[], %(path_seqs)s::int[])
                   AS u(edge_id, path_seq)
              JOIN {tbl} r ON r.{eid} = u.edge_id
             WHERE r.{geom} IS NOT NULL
        ),
        merged AS (
            SELECT ST_LineMerge(ST_Collect(geom ORDER BY path_seq)) AS line_g,
                   ST_Collect(geom ORDER BY path_seq) AS multi_g
              FROM ordered
        )
        SELECT ST_AsGeoJSON(
                 ST_Transform(
                   ST_Simplify(
                     COALESCE(
                       NULLIF(line_g, ST_GeomFromText('LINESTRING EMPTY', ST_SRID(line_g))),
                       multi_g
                     ),
                     0.00002
                   ),
                   4326
                 ),
                 5
               ) AS route_geom_json
          FROM merged
    """
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout TO 60000")
        cur.execute(sql, {"edge_ids": edge_ids, "path_seqs": path_seqs})
        row = cur.fetchone()
    if not row:
        return None
    return row.get("route_geom_json")


def point_feature_from_geom_json(row: Dict[str, Any], role: str) -> Dict[str, Any]:
    """Feature GeoJSON para origen o destino."""
    gj = (row.get("geom_json") or "").strip()
    if gj:
        geom = json.loads(gj)
    else:
        geom = {"type": "Point", "coordinates": [0, 0]}
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "role": role,
            "cvegeo": row.get("cvegeo"),
            "nombre": row.get("nombre"),
        },
    }


def build_feature_collection(
    route_geom: Dict[str, Any],
    loc_o: Dict[str, Any],
    loc_d: Dict[str, Any],
    edge_count: int,
    length_m: float,
    length_km: float,
) -> Dict[str, Any]:
    """FeatureCollection completa para la respuesta del endpoint."""
    features = [
        {
            "type": "Feature",
            "geometry": route_geom,
            "properties": {
                "role": "route",
                "edge_count": edge_count,
                "length_m": round(length_m, 2),
                "length_km": length_km,
            },
        },
        point_feature_from_geom_json(loc_o, "origen"),
        point_feature_from_geom_json(loc_d, "destino"),
    ]
    return {"type": "FeatureCollection", "features": features}
