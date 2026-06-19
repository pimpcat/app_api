"""
Ejecución de ruta: Dijkstra, stitch de corredor, geometría y resumen.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from column_resolver import resolve_column
from ruteo.routing_engine.candidates import graph_candidates
from ruteo.routing_engine.errors import RuteoError
from ruteo.routing_engine.geojson import build_route_geom_json
from ruteo.routing_engine.graph import build_graph_sql
from ruteo.routing_engine.restrictions import is_toll_edge_sql
from ruteo.routing_engine.search import fetch_dijkstra_path
from ruteo.routing_engine.sql_builder import tokens_any_match_sql
from ruteo.routing_engine.strategies.od_corridor import try_stitched_corridor_route
from ruteo.routing_engine.legacy_od_cost import sierra_nombre_match_sql
from ruteo.routing_engine.summary import build_route_resumen, sum_route_length_m
from ruteo.routing_engine.types import CostProfile, GraphVariant, RouteContext, RoutingOptions
from tables import SCHEMA, T_RNC, qualified
from utils import quote_ident


def assert_route_sin_peaje(
    conn,
    route_meta: Dict[str, Any],
    edge_ids: List[int],
) -> None:
    """Falla si la ruta «sin peajes» atraviesa tramos marcados con peaje en c_rnc."""
    if not edge_ids:
        return
    peaje_col = resolve_column(conn, SCHEMA, T_RNC, ["peaje"])
    if not peaje_col:
        return

    eid_q = quote_ident(route_meta["edge_id"])
    peaje_q = quote_ident(peaje_col)
    nombre_col = resolve_column(conn, SCHEMA, T_RNC, ["nombre"])
    nombre_q = quote_ident(nombre_col) if nombre_col else None
    nombre_sel = f", {nombre_q} AS nombre" if nombre_q else ""
    tipo_col = resolve_column(conn, SCHEMA, T_RNC, ["tipo_vial"])
    tipo_q = quote_ident(tipo_col) if tipo_col else None
    toll = is_toll_edge_sql(peaje_q, tipo_q, nombre_q)

    sql = f"""
        SELECT {eid_q} AS id{nombre_sel}
          FROM {qualified(T_RNC)}
         WHERE {eid_q} = ANY(%(ids)s)
           AND {toll}
         LIMIT 3
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"ids": edge_ids})
        bad = cur.fetchall()
    if not bad:
        return

    sample = ", ".join(
        f"{r.get('id')} ({r.get('nombre') or 'sin nombre'})" for r in bad
    )
    raise RuteoError(
        "TOLL_ROUTE",
        "La ruta calculada sin peajes aún atraviesa tramos de peaje "
        f"(ej. {sample}). No hay alternativa libre conectada en la red para este par.",
    )


def route_quality_stats(
    conn,
    route_meta: Dict[str, Any],
    edge_ids: List[int],
    route_ctx: Optional[RouteContext] = None,
) -> Dict[str, float]:
    """Métricas de calidad de ruta para comparar candidatos sin peajes."""
    if not edge_ids or not route_meta.get("intelligent_routing"):
        total = sum_route_length_m(conn, route_meta, edge_ids)
        return {
            "total_m": total,
            "unpaved_m": 0.0,
            "unnamed_m": 0.0,
            "sierra_m": 0.0,
            "corridor_m": 0.0,
        }

    tbl = route_meta["routing_table"]
    sierra = sierra_nombre_match_sql("LOWER(TRIM(COALESCE(nombre::text, '')))")
    corridor = "FALSE"
    if route_ctx and route_ctx.tokens_o and route_ctx.tokens_d:
        nom_lc = "LOWER(TRIM(COALESCE(nombre::text, '')))"
        match_o = tokens_any_match_sql(nom_lc, route_ctx.tokens_o)
        match_d = tokens_any_match_sql(nom_lc, route_ctx.tokens_d)
        corridor = f"(({match_o}) AND ({match_d}))"

    sql = f"""
        SELECT COALESCE(SUM(longitud_m), 0) AS total_m,
               COALESCE(SUM(longitud_m) FILTER (WHERE NOT es_pavimentado), 0) AS unpaved_m,
               COALESCE(SUM(longitud_m) FILTER (
                   WHERE TRIM(COALESCE(nombre::text, '')) IN ('N/D', 'N/A', '')
               ), 0) AS unnamed_m,
               COALESCE(SUM(longitud_m) FILTER (WHERE ({sierra})), 0) AS sierra_m,
               COALESCE(SUM(longitud_m) FILTER (WHERE ({corridor})), 0) AS corridor_m
          FROM {tbl}
         WHERE id = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (edge_ids,))
        row = cur.fetchone()
    if not row:
        return {
            "total_m": float("inf"),
            "unpaved_m": 0.0,
            "unnamed_m": 0.0,
            "sierra_m": 0.0,
            "corridor_m": 0.0,
        }
    return {k: float(row[k] or 0) for k in ("total_m", "unpaved_m", "unnamed_m", "sierra_m", "corridor_m")}


def route_quality_score(
    stats: Dict[str, float],
    route_ctx: Optional[RouteContext] = None,
    *,
    max_len_m: Optional[float] = None,
) -> float:
    """Menor = mejor."""
    total = stats.get("total_m") or 0.0
    if total <= 0:
        return float("inf")
    unpaved = stats.get("unpaved_m") or 0.0
    unnamed = stats.get("unnamed_m") or 0.0
    sierra = stats.get("sierra_m") or 0.0
    corridor = stats.get("corridor_m") or 0.0
    score = total + unpaved * 20.0 + unnamed * 25.0 + sierra * 120.0 - corridor * 4.0
    if max_len_m and total > max_len_m:
        score += (total - max_len_m) * 15.0
    return score


def run_route(
    conn,
    route_meta: Dict[str, Any],
    start_vid: int,
    end_vid: int,
    usar_peajes: bool,
    route_ctx: Optional[RouteContext] = None,
    loc_meta: Optional[Dict[str, str]] = None,
    cvegeo_origen: str = "",
    cvegeo_destino: str = "",
) -> Tuple[int, float, Optional[str], Dict[str, Any], List[Dict[str, Any]]]:
    """
    Fase 1: pgr_dijkstra (con o sin peajes).
    Fase 2: geometría + resumen de atributos.
    """
    path_rows: List[Dict[str, Any]] = []
    last_toll_error: Optional[RuteoError] = None
    best_score = float("inf")
    fallback_rows: Optional[List[Dict[str, Any]]] = None
    fallback_len_m = float("inf")

    best_any_rows: Optional[List[Dict[str, Any]]] = None
    best_any_score = float("inf")

    toll_ref_m: Optional[float] = None
    max_len_m = float("inf")

    options = RoutingOptions.from_usar_peajes(usar_peajes, route_ctx)

    if not usar_peajes:
        peajes_opts = RoutingOptions(
            usar_costos_materializados=True,
            cost_profile=CostProfile.MATERIALIZED,
            graph_variant=GraphVariant.TOLL_MATERIALIZED,
        )
        toll_sql = build_graph_sql(route_meta, peajes_opts)
        toll_rows = fetch_dijkstra_path(conn, toll_sql, start_vid, end_vid)
        if toll_rows:
            toll_ref_m = sum_route_length_m(
                conn, route_meta, [int(r["edge"]) for r in toll_rows]
            )
            max_len_m = toll_ref_m * 1.28 + 8_000.0

    def _accept_sin_peaje_rows(rows: List[Dict[str, Any]]) -> bool:
        nonlocal best_score, path_rows, fallback_rows, fallback_len_m
        nonlocal last_toll_error, best_any_rows, best_any_score
        edge_ids_try = [int(r["edge"]) for r in rows]
        try:
            assert_route_sin_peaje(conn, route_meta, edge_ids_try)
        except RuteoError as exc:
            if exc.code != "TOLL_ROUTE":
                raise
            last_toll_error = exc
            return False
        total_m = sum_route_length_m(conn, route_meta, edge_ids_try)
        if total_m < fallback_len_m:
            fallback_len_m = total_m
            fallback_rows = rows
        stats = route_quality_stats(conn, route_meta, edge_ids_try, route_ctx)
        score = route_quality_score(stats, route_ctx, max_len_m=max_len_m)
        if score < best_any_score:
            best_any_score = score
            best_any_rows = rows
        if score < best_score:
            best_score = score
            path_rows = rows
        return True

    def _stitched_usable(rows: List[Dict[str, Any]]) -> bool:
        edge_ids_try = [int(r["edge"]) for r in rows]
        try:
            assert_route_sin_peaje(conn, route_meta, edge_ids_try)
        except RuteoError:
            return False
        total_m = sum_route_length_m(conn, route_meta, edge_ids_try)
        return total_m > 0 and (not toll_ref_m or total_m <= max_len_m * 1.2)

    stitched_rows: Optional[List[Dict[str, Any]]] = None
    skip_global = False
    if (
        not usar_peajes
        and route_ctx
        and loc_meta
        and cvegeo_origen
        and cvegeo_destino
    ):
        stitched_rows = try_stitched_corridor_route(
            conn,
            route_meta,
            loc_meta,
            start_vid,
            end_vid,
            cvegeo_origen,
            cvegeo_destino,
            route_ctx,
        )
        if stitched_rows and _stitched_usable(stitched_rows):
            path_rows = stitched_rows
            skip_global = True

    if not skip_global:
        for edges_sql in graph_candidates(route_meta, options):
            rows = fetch_dijkstra_path(conn, edges_sql, start_vid, end_vid)
            if not rows:
                continue
            if usar_peajes:
                path_rows = rows
                break
            _accept_sin_peaje_rows(rows)

    if not path_rows and stitched_rows and _stitched_usable(stitched_rows):
        path_rows = stitched_rows

    if not path_rows and best_any_rows:
        path_rows = best_any_rows

    if not path_rows and fallback_rows:
        try:
            assert_route_sin_peaje(
                conn, route_meta, [int(r["edge"]) for r in fallback_rows]
            )
            path_rows = fallback_rows
        except RuteoError:
            pass

    if not path_rows:
        if last_toll_error:
            raise last_toll_error
        return 0, 0.0, None, {}, []

    edge_ids = [int(r["edge"]) for r in path_rows]

    geom_json = build_route_geom_json(conn, route_meta, path_rows)
    if not geom_json:
        return 0, 0.0, None, {}, path_rows

    resumen = build_route_resumen(conn, route_meta, edge_ids, usar_peajes)
    length_m = float(resumen.get("longitud_m") or 0)
    return len(path_rows), length_m, geom_json, resumen, path_rows
