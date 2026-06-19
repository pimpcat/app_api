"""
Estrategia de ruta en 3 tramos: acceso → corredor OD → acceso.

Dijkstra del tronco solo sobre el subgrafo del corredor nombrado (~cientos de aristas).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from column_resolver import resolve_column
from ruteo.routing_engine.graph import build_graph_sql
from ruteo.routing_engine.legacy_od_filters import not_toll_on_alias_sql, od_direct_nombre_lc_values
from ruteo.routing_engine.restrictions import is_toll_edge_sql
from ruteo.routing_engine.search import fetch_dijkstra_path
from ruteo.routing_engine.sql_builder import dist_geog_m_sql, dwithin_geog_m_sql
from ruteo.routing_engine.summary import sum_route_length_m
from ruteo.routing_engine.types import CostProfile, GraphVariant, RouteContext, RoutingOptions
from tables import SCHEMA, T_RNC, T_RNC_ROUTING, qualified
from utils import quote_ident

T_RNC_VERTICES = "c_rnc_vertices_pgr"

CORRIDOR_BRIDGE_MAX_M = 20_000.0
CORRIDOR_BRIDGE_WAVES = 5
STITCH_MAX_EDGES = 550
STITCH_MAX_LEN_M = 175_000.0
STITCH_ACCESS_MAX_M = 65_000.0
STITCH_TRUNK_MAX_M = 98_000.0


def _c_rnc_toll_predicate_sql(conn, *, alias: str = "c") -> str:
    peaje_col = resolve_column(conn, SCHEMA, T_RNC, ["peaje"])
    tipo_col = resolve_column(conn, SCHEMA, T_RNC, ["tipo_vial"])
    nombre_col = resolve_column(conn, SCHEMA, T_RNC, ["nombre"])
    if not peaje_col:
        return "FALSE"
    return is_toll_edge_sql(
        f"{alias}.{quote_ident(peaje_col)}",
        f"{alias}.{quote_ident(tipo_col)}" if tipo_col else None,
        f"{alias}.{quote_ident(nombre_col)}" if nombre_col else None,
    )


def fetch_corridor_named_edge_ids(
    conn,
    route_ctx: RouteContext,
) -> List[int]:
    """GIDs en c_rnc del corredor directo origen–destino (catálogo, no routing)."""
    names = od_direct_nombre_lc_values(route_ctx)
    if not names:
        return []
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    nombre_col = resolve_column(conn, SCHEMA, T_RNC, ["nombre"])
    if not edge_id or not nombre_col:
        return []
    eid_q = quote_ident(edge_id)
    nom_q = quote_ident(nombre_col)
    not_toll = f"NOT ({_c_rnc_toll_predicate_sql(conn, alias='c')})"
    sql = f"""
        SELECT c.{eid_q}::bigint AS id
          FROM {qualified(T_RNC)} c
         WHERE {not_toll}
           AND LOWER(TRIM(COALESCE(c.{nom_q}::text, ''))) = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (list(names),))
        return [int(row["id"]) for row in cur.fetchall()]


def fetch_corridor_bridge_edge_ids(
    conn,
    corridor_ids: List[int],
) -> List[int]:
    """N/D entre vértices del corredor nombrado (cierra huecos de nombre en el libre)."""
    if len(corridor_ids) < 2:
        return []
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    nombre_col = resolve_column(conn, SCHEMA, T_RNC, ["nombre"])
    if not edge_id or not nombre_col:
        return []
    eid_q = quote_ident(edge_id)
    nom_q = quote_ident(nombre_col)
    routing = qualified(T_RNC_ROUTING)
    rnc = qualified(T_RNC)
    not_toll = not_toll_on_alias_sql("r")
    max_m = CORRIDOR_BRIDGE_MAX_M
    sql = f"""
        WITH cv AS (
            SELECT r.source AS v
              FROM {routing} r
             WHERE r.id = ANY(%(ids)s)
            UNION
            SELECT r.target AS v
              FROM {routing} r
             WHERE r.id = ANY(%(ids)s)
        )
        SELECT DISTINCT r.id::bigint AS id
          FROM {routing} r
          JOIN {rnc} c ON c.{eid_q} = r.id
         WHERE r.id <> ALL(%(ids)s)
           AND ({not_toll})
           AND TRIM(COALESCE(c.{nom_q}::text, '')) IN ('N/D', 'N/A', '')
           AND r.source IN (SELECT v FROM cv)
           AND r.target IN (SELECT v FROM cv)
           AND COALESCE(r.longitud_m, 0) <= {max_m}
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"ids": corridor_ids})
        return [int(row["id"]) for row in cur.fetchall()]


def fetch_corridor_trunk_edge_ids(
    conn,
    route_ctx: RouteContext,
) -> List[int]:
    named = fetch_corridor_named_edge_ids(conn, route_ctx)
    ids = list(named)
    for _ in range(CORRIDOR_BRIDGE_WAVES):
        bridge = fetch_corridor_bridge_edge_ids(conn, ids)
        new_ids = [i for i in bridge if i not in set(ids)]
        if not new_ids:
            break
        ids.extend(new_ids)
    return ids


def corridor_anchor_candidates(
    conn,
    loc_meta: Dict[str, str],
    cvegeo: str,
    named_edge_ids: List[int],
    *,
    max_dist_m: float = 30_000.0,
    limit: int = 10,
) -> List[int]:
    """Vértices del corredor nombrado cerca de la localidad (varios candidatos)."""
    if not named_edge_ids:
        return []
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    if not edge_id:
        return []
    q_cve = quote_ident(loc_meta["cvegeo"])
    q_geom = quote_ident(loc_meta["geom"])
    tbl_loc = loc_meta["table"]
    routing = qualified(T_RNC_ROUTING)
    rnc = qualified(T_RNC)
    verts = qualified(T_RNC_VERTICES)
    eid_q = quote_ident(edge_id)
    loc_g = f"l.{q_geom}"
    vs_g = "vs.the_geom"
    vt_g = "vt.the_geom"

    sql = f"""
        WITH loc AS (
            SELECT {loc_g} AS g
              FROM {tbl_loc} l
             WHERE TRIM(l.{q_cve}::text) = %(cvegeo)s
               AND l.{q_geom} IS NOT NULL
             LIMIT 1
        ),
        near AS (
            SELECT r.source AS vid,
                   {dist_geog_m_sql(vs_g, 'loc.g')} AS d
              FROM {routing} r
              JOIN {rnc} c ON c.{eid_q} = r.id
              JOIN {verts} vs ON vs.id = r.source
              CROSS JOIN loc
             WHERE r.id = ANY(%(ids)s)
               AND {dwithin_geog_m_sql('c.the_geom', 'loc.g', max_dist_m)}
            UNION ALL
            SELECT r.target,
                   {dist_geog_m_sql(vt_g, 'loc.g')}
              FROM {routing} r
              JOIN {rnc} c ON c.{eid_q} = r.id
              JOIN {verts} vt ON vt.id = r.target
              CROSS JOIN loc
             WHERE r.id = ANY(%(ids)s)
               AND {dwithin_geog_m_sql('c.the_geom', 'loc.g', max_dist_m)}
        )
        SELECT vid::int AS vid
          FROM (
            SELECT vid, MIN(d) AS d
              FROM near
             GROUP BY vid
             ORDER BY d
             LIMIT %(limit)s
          ) q
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            {"cvegeo": cvegeo, "ids": named_edge_ids, "limit": limit},
        )
        return [int(row["vid"]) for row in cur.fetchall()]


def resolve_connected_corridor_anchors(
    conn,
    route_meta: Dict[str, Any],
    loc_meta: Dict[str, str],
    cvegeo_o: str,
    cvegeo_d: str,
    route_ctx: RouteContext,
    trunk_ids: List[int],
    trunk_sql: str,
) -> Optional[Tuple[int, int, List[Dict[str, Any]]]]:
    """Par de anclas en el subgrafo con tronco conectado (prueba candidatos cercanos)."""
    named = fetch_corridor_named_edge_ids(conn, route_ctx)
    cands_o = corridor_anchor_candidates(conn, loc_meta, cvegeo_o, named)
    cands_d = corridor_anchor_candidates(conn, loc_meta, cvegeo_d, named)
    if not cands_o or not cands_d:
        return None

    best: Optional[Tuple[int, int, List[Dict[str, Any]]]] = None
    best_m = float("inf")
    for vo in cands_o:
        for vd in cands_d:
            if vo == vd:
                continue
            rows = fetch_dijkstra_path(conn, trunk_sql, vo, vd)
            if not rows:
                continue
            leg_m = sum_route_length_m(
                conn, route_meta, [int(r["edge"]) for r in rows]
            )
            if leg_m > STITCH_TRUNK_MAX_M or leg_m >= best_m:
                continue
            best_m = leg_m
            best = (vo, vd, rows)
    return best


def snap_vertex_to_corridor(
    conn,
    loc_meta: Dict[str, str],
    cvegeo: str,
    route_ctx: RouteContext,
    *,
    max_dist_m: float = 25_000.0,
) -> Optional[int]:
    """Vértice en el corredor OD (nombre exacto en c_rnc) cerca de la localidad."""
    if not route_ctx.tokens_o or not route_ctx.tokens_d:
        return None
    names = od_direct_nombre_lc_values(route_ctx)
    if not names:
        return None
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    nombre_col = resolve_column(conn, SCHEMA, T_RNC, ["nombre"])
    if not edge_id or not nombre_col:
        return None

    q_cve = quote_ident(loc_meta["cvegeo"])
    q_geom = quote_ident(loc_meta["geom"])
    tbl_loc = loc_meta["table"]
    routing = qualified(T_RNC_ROUTING)
    rnc = qualified(T_RNC)
    verts = qualified(T_RNC_VERTICES)
    eid_q = quote_ident(edge_id)
    nom_q = quote_ident(nombre_col)
    not_toll = f"NOT ({_c_rnc_toll_predicate_sql(conn, alias='c')})"
    loc_g = f"l.{q_geom}"
    vs_g = "vs.the_geom"
    vt_g = "vt.the_geom"
    near_src = f"ST_Distance({vs_g}, {loc_g}) <= ST_Distance({vt_g}, {loc_g})"
    near_geom = f"CASE WHEN {near_src} THEN {vs_g} ELSE {vt_g} END"

    sql = f"""
        SELECT CASE WHEN {near_src} THEN r.source ELSE r.target END::int AS vid,
               {dist_geog_m_sql(near_geom, loc_g)} AS dist_m
          FROM {routing} r
          JOIN {rnc} c ON c.{eid_q} = r.id
          JOIN {verts} vs ON vs.id = r.source
          JOIN {verts} vt ON vt.id = r.target
          JOIN {tbl_loc} l ON TRIM(l.{q_cve}::text) = %(cvegeo)s
         WHERE l.{q_geom} IS NOT NULL
           AND {not_toll}
           AND LOWER(TRIM(COALESCE(c.{nom_q}::text, ''))) = ANY(%(names)s)
           AND {dwithin_geog_m_sql('c.the_geom', loc_g, max_dist_m)}
         ORDER BY c.the_geom <-> l.{q_geom}
         LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"cvegeo": cvegeo, "names": list(names)})
        row = cur.fetchone()
    if row and row.get("vid") is not None:
        return int(row["vid"])
    return None


def merge_dijkstra_paths(parts: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seq = 0
    for part in parts:
        for row in part:
            merged.append(
                {
                    "edge": int(row["edge"]),
                    "path_seq": seq,
                    "agg_cost": row.get("agg_cost"),
                }
            )
            seq += 1
    return merged


def _corridor_subgraph_sql(
    route_meta: Dict[str, Any],
    route_ctx: RouteContext,
    edge_ids: List[int],
) -> Optional[str]:
    """Subgrafo pequeño del corredor (Dijkstra rápido)."""
    if not edge_ids:
        return None
    options = RoutingOptions(
        evitar_peajes=True,
        cost_profile=CostProfile.DISTANCE_OD,
        graph_variant=GraphVariant.CORRIDOR_SUBGRAPH,
        route_context=route_ctx,
    )
    return build_graph_sql(
        route_meta,
        options,
        corridor_edge_ids=edge_ids,
    )


def try_stitched_corridor_route(
    conn,
    route_meta: Dict[str, Any],
    loc_meta: Dict[str, str],
    start_vid: int,
    end_vid: int,
    cvegeo_o: str,
    cvegeo_d: str,
    route_ctx: RouteContext,
) -> Optional[List[Dict[str, Any]]]:
    """
    Ruta en 3 tramos: acceso → subgrafo corredor (c_rnc) → acceso.
    Dijkstra del tronco solo sobre ~cientos de aristas, no 200k.
    """
    routing_tbl = route_meta.get("routing_table")
    if not routing_tbl or not route_meta.get("intelligent_routing"):
        return None

    trunk_ids = fetch_corridor_trunk_edge_ids(conn, route_ctx)
    trunk_sql = _corridor_subgraph_sql(route_meta, route_ctx, trunk_ids)
    if not trunk_sql:
        return None

    resolved = resolve_connected_corridor_anchors(
        conn,
        route_meta,
        loc_meta,
        cvegeo_o,
        cvegeo_d,
        route_ctx,
        trunk_ids,
        trunk_sql,
    )
    if not resolved:
        return None
    anchor_o, anchor_d, trunk_rows = resolved

    access_options_plain = RoutingOptions(
        evitar_peajes=True,
        cost_profile=CostProfile.DISTANCE_OD,
        graph_variant=GraphVariant.ACCESS_PLAIN,
        route_context=route_ctx,
    )
    access_options_no_toll = RoutingOptions(
        evitar_peajes=True,
        cost_profile=CostProfile.DISTANCE_OD,
        graph_variant=GraphVariant.ACCESS_NO_TOLL,
        route_context=route_ctx,
    )
    access_variants = [
        build_graph_sql(route_meta, access_options_plain),
        build_graph_sql(route_meta, access_options_no_toll),
    ]

    def _leg_length_ok(rows: List[Dict[str, Any]], max_m: float) -> bool:
        if not rows:
            return False
        leg_m = sum_route_length_m(
            conn, route_meta, [int(r["edge"]) for r in rows]
        )
        return leg_m <= max_m

    if not _leg_length_ok(trunk_rows, STITCH_TRUNK_MAX_M):
        return None

    for access_sql in access_variants:
        parts: List[List[Dict[str, Any]]] = []
        ok = True
        for a, b, sql, leg_max, preset in (
            (start_vid, anchor_o, access_sql, STITCH_ACCESS_MAX_M, None),
            (anchor_o, anchor_d, trunk_sql, STITCH_TRUNK_MAX_M, trunk_rows),
            (anchor_d, end_vid, access_sql, STITCH_ACCESS_MAX_M, None),
        ):
            if a == b:
                continue
            rows = preset if preset is not None else fetch_dijkstra_path(conn, sql, a, b)
            if not rows or not _leg_length_ok(rows, leg_max):
                ok = False
                break
            parts.append(rows)
        if not ok or not parts:
            continue
        merged = merge_dijkstra_paths(parts)
        if len(merged) > STITCH_MAX_EDGES:
            continue
        stitch_m = sum_route_length_m(
            conn, route_meta, [int(r["edge"]) for r in merged]
        )
        if stitch_m > STITCH_MAX_LEN_M:
            continue
        return merged
    return None
