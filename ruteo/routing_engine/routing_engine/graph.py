"""
Construcción del grafo pgRouting (consulta SQL de aristas).

``build_graph_sql`` ensambla el SELECT que consume ``pgr_dijkstra``:
id, source, target, cost, reverse_cost — sin geometría.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ruteo.routing_engine.costs import build_cost_sql
from ruteo.routing_engine.legacy_od_filters import graph_where_for_variant
from ruteo.routing_engine.restrictions import build_where_sql
from ruteo.routing_engine.scoring import build_exclusion_where_sql
from ruteo.routing_engine.types import ColumnSet, CostProfile, GraphVariant, RoutingOptions


def edges_sql_with_cost(
    routing_tbl: str,
    cost_expr: str,
    where: str = "",
) -> str:
    """Grafo pgRouting con costo en c_rnc_routing (sin JOIN)."""
    sql = (
        f"SELECT r.id, r.source, r.target, {cost_expr} AS cost, "
        f"{cost_expr} AS reverse_cost "
        f"FROM {routing_tbl} r"
    )
    if where:
        sql += f" WHERE {where}"
    return sql


def build_graph_sql(
    route_meta: Dict[str, Any],
    options: RoutingOptions,
    *,
    alias: str = "r",
    variant: Optional[GraphVariant] = None,
    extra_where: Optional[str] = None,
    legacy_od_cost_extension: Optional[str] = None,
    table_override: Optional[str] = None,
    corridor_edge_ids: Optional[List[int]] = None,
) -> str:
    """
    Genera el SQL del grafo para ``pgr_dijkstra``.

    Usa ``GraphVariant`` y ``legacy_od_filters.graph_where_for_variant`` para
    los modos legacy sin peajes; ``CostProfile.MATERIALIZED`` devuelve el
    SELECT ligero sobre costos precalculados.
    """
    routing_tbl = table_override or route_meta.get("routing_table")
    columns = ColumnSet.from_route_meta(route_meta)
    graph_variant = variant or options.graph_variant

    if options.cost_profile == CostProfile.MATERIALIZED and routing_tbl:
        if graph_variant == GraphVariant.TOLL_MATERIALIZED:
            sql = (
                f"SELECT id, source, target, cost, reverse_cost "
                f"FROM {routing_tbl}"
            )
            where = build_where_sql(options, columns, alias="", restrictions=())
            if where:
                sql += f" WHERE {where}"
            return sql

        variant_where = graph_where_for_variant(
            graph_variant,
            options.route_context,
            alias=alias,
            corridor_edge_ids=corridor_edge_ids,
        )
        if variant_where:
            return (
                f"SELECT {alias}.id, {alias}.source, {alias}.target, "
                f"{alias}.cost, {alias}.reverse_cost "
                f"FROM {routing_tbl} {alias} "
                f"WHERE {variant_where}"
            )
        return (
            f"SELECT id, source, target, cost, reverse_cost "
            f"FROM {routing_tbl}"
        )

    if not routing_tbl:
        return route_meta["edges_sql"]

    cost_sql = build_cost_sql(
        options,
        columns,
        alias=alias,
        legacy_od_extension=legacy_od_cost_extension,
    )

    where_parts: List[str] = []
    variant_where = graph_where_for_variant(
        graph_variant,
        options.route_context,
        alias=alias,
        corridor_edge_ids=corridor_edge_ids,
    )
    if variant_where:
        where_parts.append(variant_where)

    if options.cost_profile == CostProfile.SCORING:
        exclusion = build_exclusion_where_sql(columns, alias, options)
        if exclusion:
            where_parts.append(exclusion)

        built_where = build_where_sql(
            options, columns, alias=alias, extra_where=extra_where
        )
        if built_where:
            where_parts.append(built_where)

    if extra_where:
        where_parts.append(f"({extra_where})")

    where_clause = " AND ".join(where_parts)
    sql = (
        f"SELECT {alias}.id, {alias}.source, {alias}.target, "
        f"{cost_sql.cost} AS cost, {cost_sql.reverse_cost} AS reverse_cost "
        f"FROM {routing_tbl} {alias}"
    )
    if where_clause:
        sql += f" WHERE {where_clause}"
    return sql
