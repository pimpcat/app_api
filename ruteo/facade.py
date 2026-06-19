"""
Fachada pública del módulo de ruteo RNC.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ruteo.routing_engine.cache import (
    SCHEMA_SNAPSHOT_VERSION,
    cached_schema_snapshot,
)
from ruteo.routing_engine.candidates import graph_candidates
from ruteo.routing_engine.errors import RuteoError
from ruteo.routing_engine.graph import build_graph_sql, edges_sql_with_cost
from ruteo.routing_engine.localities import buscar_localidades_rnc, fetch_localidades_par
from ruteo.routing_engine.runner import run_route
from ruteo.routing_engine.schema import (
    ensure_routing_tables_synced,
    loc_meta,
    routing_meta,
    vertices_meta,
)
from ruteo.routing_engine.search import fetch_dijkstra_path
from ruteo.routing_engine.sql_builder import len_geog_m_sql
from ruteo.routing_engine.strategies.legacy_sin_peaje import (
    extract_localidad_tokens as _extract_localidad_tokens,
    make_route_context,
)
from ruteo.routing_engine.strategies.od_corridor import (
    corridor_anchor_candidates,
    fetch_corridor_named_edge_ids,
    fetch_corridor_trunk_edge_ids,
    resolve_connected_corridor_anchors,
    snap_vertex_to_corridor,
    try_stitched_corridor_route,
)
from ruteo.routing_engine.summary import sum_route_length_m
from ruteo.routing_engine.types import CostProfile, GraphVariant, RouteContext, RoutingOptions
from ruteo.routing_engine.vertices import resolve_vertex_ids

_SCHEMA_SNAPSHOT_VERSION = SCHEMA_SNAPSHOT_VERSION
_cached_schema_snapshot = cached_schema_snapshot
_cached_loc_meta = __import__(
    "ruteo.routing_engine.cache", fromlist=["cached_loc_meta"]
).cached_loc_meta
_RouteContext = RouteContext
_len_geog_m_sql = len_geog_m_sql
_fetch_localidades_par = fetch_localidades_par
_make_route_context = make_route_context
_fetch_dijkstra_path = fetch_dijkstra_path
_run_route = run_route
_sum_route_length_m = sum_route_length_m
_resolve_vertex_ids = resolve_vertex_ids
_snap_vertex_to_corridor = snap_vertex_to_corridor
_corridor_anchor_candidates = corridor_anchor_candidates
_fetch_corridor_named_edge_ids = fetch_corridor_named_edge_ids
_fetch_corridor_trunk_edge_ids = fetch_corridor_trunk_edge_ids
_resolve_connected_corridor_anchors = resolve_connected_corridor_anchors
_try_stitched_corridor_route = try_stitched_corridor_route


def _routing_meta(conn) -> Dict[str, Any]:
    return routing_meta(conn)


def _vertices_meta(conn) -> Dict[str, str]:
    return vertices_meta(conn)


def _loc_meta(conn) -> Dict[str, str]:
    return loc_meta(conn)


def _ensure_routing_tables_synced(conn) -> None:
    ensure_routing_tables_synced(conn)


def _edges_sql(route_meta: Dict[str, Any], usar_peajes: bool) -> str:
    if usar_peajes:
        return route_meta.get("edges_sql_peajes") or route_meta["edges_sql"]
    if route_meta.get("edges_sql_sin_peaje"):
        return route_meta["edges_sql_sin_peaje"]
    return route_meta["edges_sql"]


def _edges_sql_candidates(
    route_meta: Dict[str, Any],
    usar_peajes: bool,
    route_ctx: Optional[RouteContext] = None,
) -> List[str]:
    options = RoutingOptions.from_usar_peajes(usar_peajes, route_ctx)
    return graph_candidates(route_meta, options)


def _routing_edges_sql_corridor_subgraph(
    routing_tbl: str,
    route_ctx: RouteContext,
    edge_ids: List[int],
) -> Optional[str]:
    if not edge_ids:
        return None
    route_meta = dict(cached_schema_snapshot()["route"])
    route_meta["routing_table"] = routing_tbl
    options = RoutingOptions(
        evitar_peajes=True,
        cost_profile=CostProfile.DISTANCE_OD,
        graph_variant=GraphVariant.CORRIDOR_SUBGRAPH,
        route_context=route_ctx,
    )
    return build_graph_sql(route_meta, options, corridor_edge_ids=edge_ids)


def _routing_edges_sql_with_cost(
    routing_tbl: str,
    cost_expr: str,
    where: str = "",
) -> str:
    return edges_sql_with_cost(routing_tbl, cost_expr, where)


def calcular_ruta_rnc(
    cvegeo_origen: str,
    cvegeo_destino: str,
    usar_peajes: bool = True,
) -> Dict[str, Any]:
    from ruteo.routing_engine.engine import calcular_ruta_rnc as _calcular

    return _calcular(cvegeo_origen, cvegeo_destino, usar_peajes=usar_peajes)
