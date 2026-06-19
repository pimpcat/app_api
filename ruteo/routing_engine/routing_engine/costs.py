"""
Construcción dinámica de ``cost`` y ``reverse_cost`` mediante el scoring engine.

``build_cost_sql`` despacha según ``CostProfile`` (scoring, legacy sin peaje,
distancia OD o costos materializados).
"""

from __future__ import annotations

from typing import Optional

from ruteo.routing_engine import scoring_config as cfg
from ruteo.routing_engine.legacy_od_cost import distance_no_toll_cost_sql, sin_peaje_cost_sql
from ruteo.routing_engine.scoring import (
    apply_circulation_forward_cost,
    apply_circulation_reverse_cost,
    assemble_scoring_multiplier_sql,
)
from ruteo.routing_engine.sql_builder import col_ref
from ruteo.routing_engine.types import ColumnSet, CostMode, CostProfile, CostSql, RoutingOptions


def base_cost_sql(options: RoutingOptions, columns: ColumnSet, alias: str) -> str:
    """
    Costo base por arista.

    - **distancia**: longitud en metros (``longitud_m`` o fallback).
    - **tiempo**: longitud en metros; el tiempo efectivo lo aporta ``factor_velocidad``.
    """
    if columns.longitud_m:
        length = f"GREATEST(COALESCE({col_ref(alias, columns.longitud_m)}, 0), 1.0)"
    elif columns.cost_col:
        length = f"GREATEST(COALESCE({col_ref(alias, columns.cost_col)}, 0), 1.0)"
    else:
        length = "1.0"

    if options.modo == CostMode.DISTANCIA:
        return length
    return length


def build_cost_sql(
    options: RoutingOptions,
    columns: ColumnSet,
    *,
    alias: str = "r",
    legacy_od_extension: Optional[str] = None,
) -> CostSql:
    """
    Genera expresiones SQL para ``cost`` y ``reverse_cost`` de pgRouting.

    Despacha por ``CostProfile``; el perfil ``SCORING`` usa el motor multiplicativo.
    """
    profile = options.cost_profile

    if profile == CostProfile.MATERIALIZED:
        return CostSql(cost="cost", reverse_cost="reverse_cost")

    if profile == CostProfile.LEGACY_SIN_PEAJE:
        expr = sin_peaje_cost_sql(alias=alias, route_ctx=options.route_context)
        return CostSql(cost=expr, reverse_cost=expr)

    if profile == CostProfile.DISTANCE_OD:
        expr = distance_no_toll_cost_sql(alias=alias, route_ctx=options.route_context)
        return CostSql(cost=expr, reverse_cost=expr)

    base = base_cost_sql(options, columns, alias)
    multipliers = assemble_scoring_multiplier_sql(columns, alias, options)
    scored = f"GREATEST(({base}) * ({multipliers}), {cfg.COSTO_MINIMO})"

    if legacy_od_extension:
        scored = f"GREATEST(({scored}) + ({legacy_od_extension}), {cfg.COSTO_MINIMO})"

    cost = apply_circulation_forward_cost(scored, columns, alias, options)
    reverse = apply_circulation_reverse_cost(cost, columns, alias, options)
    return CostSql(cost=cost, reverse_cost=reverse)
