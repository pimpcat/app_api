"""
Exportación GeoJSON para diagnóstico del subgrafo del corredor OD.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ruteo.routing_engine.diagnostics.corridor_subgraph import (
    ANCHOR_COLOR,
    BRIDGE_ND_COLOR,
    CANDIDATE_COLOR,
    GAP_COLOR,
    MISSING_LINK_COLOR,
    CorridorSubgraphReport,
)
from ruteo.routing_engine.diagnostics.spatial import SpatialContext, detect_spatial_context_conn
from tables import SCHEMA, T_RNC_VERTICES, qualified


def _geom_from_json(geom_json: Optional[str]) -> Optional[Dict[str, Any]]:
    if not geom_json:
        return None
    try:
        return json.loads(geom_json)
    except (TypeError, json.JSONDecodeError):
        return None


def _line_feature(
    geometry: Optional[Dict[str, Any]],
    properties: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not geometry:
        return None
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _point_feature(
    coordinates: List[float],
    properties: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coordinates},
        "properties": properties,
    }


def _fetch_vertex_coords(
    conn,
    vertex_ids: List[int],
    spatial: SpatialContext,
) -> Dict[int, List[float]]:
    if not vertex_ids:
        return {}
    verts = qualified(T_RNC_VERTICES)
    lon_sql, lat_sql = spatial.wgs84_xy_sql("the_geom")
    sql = f"""
        SELECT id::int AS id,
               {lon_sql}::double precision AS lon,
               {lat_sql}::double precision AS lat
          FROM {verts}
         WHERE id = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (vertex_ids,))
        return {
            int(row["id"]): [float(row["lon"]), float(row["lat"])]
            for row in cur.fetchall()
        }


def _gap_line_features(
    conn,
    report: CorridorSubgraphReport,
    spatial: SpatialContext,
) -> List[Dict[str, Any]]:
    features: List[Dict[str, Any]] = []
    vids: List[int] = []
    for pair in report.component_distances:
        if pair.get("nearest_vertex_a"):
            vids.append(int(pair["nearest_vertex_a"]))
        if pair.get("nearest_vertex_b"):
            vids.append(int(pair["nearest_vertex_b"]))
    coords = _fetch_vertex_coords(conn, list(set(vids)), spatial)
    for pair in report.component_distances:
        va = pair.get("nearest_vertex_a")
        vb = pair.get("nearest_vertex_b")
        if va is None or vb is None:
            continue
        ca, cb = coords.get(int(va)), coords.get(int(vb))
        if not ca or not cb:
            continue
        feat = _line_feature(
            {"type": "LineString", "coordinates": [ca, cb]},
            {
                "layer": "component_gap",
                "component_a": pair["component_a"],
                "component_b": pair["component_b"],
                "distance_m": pair["distance_m"],
                "nearest_vertex_a": va,
                "nearest_vertex_b": vb,
                "stroke": GAP_COLOR,
                "stroke-width": 3,
                "stroke-dasharray": "6,4",
            },
        )
        if feat:
            features.append(feat)
    return features


def report_to_geojson(
    conn,
    report: CorridorSubgraphReport,
) -> Dict[str, Any]:
    """FeatureCollection con estilos por capa para QGIS / visor web."""
    spatial = detect_spatial_context_conn(conn)
    features: List[Dict[str, Any]] = []

    for edge in report.edges:
        geom = _geom_from_json(edge.get("geom_json"))
        role = edge.get("edge_role", "named")
        is_bridge = role == "bridge_nd"
        props = {
            "layer": "bridge_nd" if is_bridge else "corridor_edge",
            "edge_id": int(edge["edge_id"]),
            "component_id": edge.get("component_id"),
            "edge_role": role,
            "nombre": edge.get("nombre"),
            "longitud_m": edge.get("longitud_m"),
            "source": edge.get("source"),
            "target": edge.get("target"),
            "stroke": BRIDGE_ND_COLOR if is_bridge else edge.get("color"),
            "stroke-width": 5 if is_bridge else 3,
            "stroke-dasharray": "8,5" if is_bridge else None,
            "stroke-opacity": 0.95,
        }
        feat = _line_feature(geom, props)
        if feat:
            features.append(feat)

    anchor_vids: List[int] = []
    for side in ("origen", "destino"):
        info = report.anchors.get(side, {})
        if info.get("snap_vertex_id"):
            anchor_vids.append(int(info["snap_vertex_id"]))
        for c in info.get("candidates") or []:
            anchor_vids.append(int(c["vertex_id"]))
    vcoords = _fetch_vertex_coords(conn, list(set(anchor_vids)), spatial)

    for side in ("origen", "destino"):
        info = report.anchors.get(side, {})
        snap = info.get("snap_vertex_id")
        if snap and snap in vcoords:
            features.append(
                _point_feature(
                    vcoords[int(snap)],
                    {
                        "layer": "anchor_snap",
                        "role": side,
                        "vertex_id": int(snap),
                        "component_id": info.get("snap_component_id"),
                        "marker-size": "large",
                        "marker-color": ANCHOR_COLOR,
                        "marker-symbol": "star",
                        "stroke": "#000000",
                        "stroke-width": 2,
                    },
                )
            )
        for c in info.get("candidates") or []:
            vid = int(c["vertex_id"])
            if vid in vcoords and vid != snap:
                features.append(
                    _point_feature(
                        vcoords[vid],
                        {
                            "layer": "anchor_candidate",
                            "role": side,
                            "vertex_id": vid,
                            "component_id": c.get("component_id"),
                            "marker-size": "small",
                            "marker-color": ANCHOR_COLOR,
                            "marker-symbol": "circle",
                            "stroke": "#333333",
                            "stroke-width": 1,
                        },
                    )
                )

    for gap in report.continuity_gaps:
        geom = _geom_from_json(gap.get("geom_json"))
        feat = _line_feature(
            geom,
            {
                "layer": "continuity_gap",
                "gid": gap["gid"],
                "componente_origen": gap.get("componente_origen"),
                "componente_destino": gap.get("componente_destino"),
                "distancia_entre_componentes": gap.get("distancia_entre_componentes"),
                "nombre": gap.get("nombre"),
                "tipo_vial": gap.get("tipo_vial"),
                "peaje": gap.get("peaje"),
                "longitud": gap.get("longitud"),
                "administracion": gap.get("administracion"),
                "stroke": MISSING_LINK_COLOR,
                "stroke-width": 4,
                "stroke-dasharray": "2,6",
            },
        )
        if feat:
            features.append(feat)

    for cand in report.candidate_join_edges:
        geom = _geom_from_json(cand.get("geom_json"))
        feat = _line_feature(
            geom,
            {
                "layer": "candidate_join",
                "gid": cand["gid"],
                "componente_origen": cand.get("componente_origen"),
                "componente_destino": cand.get("componente_destino"),
                "distancia_entre_componentes": cand.get("distancia_entre_componentes"),
                "nombre": cand.get("nombre"),
                "tipo_vial": cand.get("tipo_vial"),
                "peaje": cand.get("peaje"),
                "longitud": cand.get("longitud"),
                "administracion": cand.get("administracion"),
                "stroke": CANDIDATE_COLOR,
                "stroke-width": 4,
            },
        )
        if feat:
            features.append(feat)

    for ex in report.excluded_named_edges[:40]:
        geom = _geom_from_json(ex.get("geom_json"))
        feat = _line_feature(
            geom,
            {
                "layer": "excluded_named",
                "edge_id": ex["edge_id"],
                "nombre": ex.get("nombre"),
                "reason": ex.get("reason"),
                "peaje": ex.get("peaje"),
                "longitud_m": ex.get("longitud_m"),
                "stroke": "#ff9896",
                "stroke-width": 2,
                "stroke-dasharray": "4,4",
                "stroke-opacity": 0.7,
            },
        )
        if feat:
            features.append(feat)

    features.extend(_gap_line_features(conn, report, spatial))

    return {
        "type": "FeatureCollection",
        "properties": {
            "diagnostic": "corridor_subgraph",
            "schema": SCHEMA,
            "spatial_srid": spatial.srid,
            "spatial_is_projected": spatial.is_projected,
            "summary": report.to_dict(),
        },
        "features": features,
    }
