"""
Lista ordenada de grafos SQL a probar en Dijkstra (modo con/sin peajes).
"""

from __future__ import annotations

from typing import Any, Dict, List

from ruteo.routing_engine.graph import build_graph_sql
from ruteo.routing_engine.types import CostMode, CostProfile, GraphVariant, RouteContext, RoutingOptions


def _legacy_options(
    route_ctx: RouteContext | None,
    *,
    profile: CostProfile,
    variant: GraphVariant,
) -> RoutingOptions:
    return RoutingOptions(
        modo=CostMode.TIEMPO,
        evitar_peajes=True,
        evitar_terracerias=True,
        evitar_construccion=True,
        usar_costos_materializados=profile == CostProfile.MATERIALIZED,
        cost_profile=profile,
        graph_variant=variant,
        route_context=route_ctx,
    )


def _edges_sql(route_meta: Dict[str, Any], usar_peajes: bool) -> str:
    """SQL de aristas según preferencia de peajes (un solo grafo)."""
    if usar_peajes:
        return route_meta.get("edges_sql_peajes") or route_meta["edges_sql"]
    if route_meta.get("edges_sql_sin_peaje"):
        return route_meta["edges_sql_sin_peaje"]
    return route_meta["edges_sql"]


def _build_sin_peaje_edge_candidates(
    route_meta: Dict[str, Any],
    route_ctx: RouteContext,
) -> List[str]:
    routing_tbl = route_meta.get("routing_table")
    if not routing_tbl:
        return []
    return [
        build_graph_sql(
            route_meta,
            _legacy_options(route_ctx, profile=CostProfile.LEGACY_SIN_PEAJE, variant=GraphVariant.SIN_PEAJE_NO_TOLL),
        ),
        build_graph_sql(
            route_meta,
            _legacy_options(route_ctx, profile=CostProfile.LEGACY_SIN_PEAJE, variant=GraphVariant.SIN_PEAJE_FULL),
        ),
    ]


def graph_candidates(
    route_meta: Dict[str, Any],
    options: RoutingOptions,
) -> List[str]:
    """Lista de grafos a probar (sin peajes: costo dinámico por par origen–destino)."""
    usar_peajes = not options.evitar_peajes
    route_ctx = options.route_context

    if usar_peajes:
        peajes_opts = RoutingOptions(
            usar_costos_materializados=True,
            cost_profile=CostProfile.MATERIALIZED,
            graph_variant=GraphVariant.TOLL_MATERIALIZED,
        )
        return [build_graph_sql(route_meta, peajes_opts)]

    if route_ctx and route_meta.get("intelligent_routing"):
        return _build_sin_peaje_edge_candidates(route_meta, route_ctx)

    candidates: List[str] = []
    if route_meta.get("intelligent_routing"):
        paved_opts = _legacy_options(
            route_ctx, profile=CostProfile.LEGACY_SIN_PEAJE, variant=GraphVariant.SIN_PEAJE_PAVED
        )
        candidates.append(build_graph_sql(route_meta, paved_opts))
        no_toll_opts = _legacy_options(
            route_ctx, profile=CostProfile.LEGACY_SIN_PEAJE, variant=GraphVariant.SIN_PEAJE_NO_TOLL
        )
        no_toll_sql = build_graph_sql(route_meta, no_toll_opts)
        if no_toll_sql not in candidates:
            candidates.append(no_toll_sql)

    sin_peaje = route_meta.get("edges_sql_sin_peaje")
    if sin_peaje and sin_peaje not in candidates:
        candidates.append(sin_peaje)

    if not candidates:
        if route_meta.get("routing_table"):
            profile = (
                CostProfile.LEGACY_SIN_PEAJE
                if route_meta.get("intelligent_routing")
                else CostProfile.MATERIALIZED
            )
            fallback_opts = _legacy_options(
                route_ctx,
                profile=profile,
                variant=GraphVariant.SIN_PEAJE_FULL,
            )
            candidates.append(build_graph_sql(route_meta, fallback_opts))
        else:
            candidates.append(_edges_sql(route_meta, False))

    return candidates
