"""
Resolución de vértices pgRouting para localidades.

Usa ``node_id`` precalculado en ``c_rnc_loc``; solo consulta KNN
cuando falta el nodo asociado.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ruteo.routing_engine.errors import RuteoError
from ruteo.routing_engine.types import RouteContext
from utils import quote_ident


def parse_node_id(node_raw: Any) -> Optional[int]:
    if node_raw is None:
        return None
    s = str(node_raw).strip()
    if not s or s == "0":
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def resolve_vertex_ids(
    conn,
    loc_meta: Dict[str, str],
    vert_meta: Dict[str, str],
    loc_rows: Dict[str, Dict[str, Any]],
    cvegeo_origen: str,
    cvegeo_destino: str,
    *,
    usar_peajes: bool = True,
    route_ctx: Optional[RouteContext] = None,
    snap_to_corridor_fn=None,
) -> Tuple[int, int]:
    """
    Obtiene ``start_vid`` y ``end_vid`` para pgRouting.

    ``snap_to_corridor_fn`` opcional: refina vértices en modo sin peajes
    (estrategia legacy OD); se inyecta para evitar dependencia circular.
    """
    loc_o = loc_rows[cvegeo_origen]
    loc_d = loc_rows[cvegeo_destino]
    start_vid = parse_node_id(loc_o.get("node_id"))
    end_vid = parse_node_id(loc_d.get("node_id"))

    need_o = start_vid is None
    need_d = end_vid is None
    if need_o or need_d:
        q_geom = quote_ident(loc_meta["geom"])
        v_id = quote_ident(vert_meta["id"])
        v_geom = quote_ident(vert_meta["geom"])
        q_cve = quote_ident(loc_meta["cvegeo"])
        tbl_loc = loc_meta["table"]
        tbl_vert = vert_meta["table"]

        if need_o and need_d:
            sql = f"""
                SELECT
                  (SELECT v.{v_id}
                     FROM {tbl_vert} v
                     JOIN {tbl_loc} l ON TRIM(l.{q_cve}::text) = %(origen)s
                    WHERE l.{q_geom} IS NOT NULL AND v.{v_geom} IS NOT NULL
                    ORDER BY v.{v_geom} <-> l.{q_geom}
                    LIMIT 1) AS start_vid,
                  (SELECT v.{v_id}
                     FROM {tbl_vert} v
                     JOIN {tbl_loc} l ON TRIM(l.{q_cve}::text) = %(destino)s
                    WHERE l.{q_geom} IS NOT NULL AND v.{v_geom} IS NOT NULL
                    ORDER BY v.{v_geom} <-> l.{q_geom}
                    LIMIT 1) AS end_vid
            """
            params = {"origen": cvegeo_origen, "destino": cvegeo_destino}
        elif need_o:
            sql = f"""
                SELECT v.{v_id} AS start_vid
                  FROM {tbl_vert} v
                  JOIN {tbl_loc} l ON TRIM(l.{q_cve}::text) = %(origen)s
                 WHERE l.{q_geom} IS NOT NULL AND v.{v_geom} IS NOT NULL
                 ORDER BY v.{v_geom} <-> l.{q_geom}
                 LIMIT 1
            """
            params = {"origen": cvegeo_origen}
        else:
            sql = f"""
                SELECT v.{v_id} AS end_vid
                  FROM {tbl_vert} v
                  JOIN {tbl_loc} l ON TRIM(l.{q_cve}::text) = %(destino)s
                 WHERE l.{q_geom} IS NOT NULL AND v.{v_geom} IS NOT NULL
                 ORDER BY v.{v_geom} <-> l.{q_geom}
                 LIMIT 1
            """
            params = {"destino": cvegeo_destino}

        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()

        if need_o:
            start_vid = (
                int(row.get("start_vid"))
                if row and row.get("start_vid") is not None
                else None
            )
        if need_d:
            end_vid = (
                int(row.get("end_vid"))
                if row and row.get("end_vid") is not None
                else None
            )

    if start_vid is None:
        raise RuteoError(
            "VERTEX_NOT_FOUND",
            f"No se pudo asociar la localidad {cvegeo_origen} a la red vial.",
        )
    if end_vid is None:
        raise RuteoError(
            "VERTEX_NOT_FOUND",
            f"No se pudo asociar la localidad {cvegeo_destino} a la red vial.",
        )

    if not usar_peajes and route_ctx and snap_to_corridor_fn:
        start_vid, end_vid = snap_to_corridor_fn(
            conn, loc_meta, cvegeo_origen, cvegeo_destino, route_ctx, start_vid, end_vid
        )

    return start_vid, end_vid
