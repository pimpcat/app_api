"""
Filtros WHERE SQL legacy para grafos sin peajes y corredor OD.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ruteo.routing_engine.legacy_od_cost import (
    detour_nombre_match_sql,
    es_pavimentado_ruteo_sql,
    is_infrastructure_connector_sql,
    sierra_nombre_match_sql,
)
from ruteo.routing_engine.restrictions import CONNECTOR_MAX_LEN_M, is_toll_edge_sql
from ruteo.routing_engine.sql_builder import col_ref, tokens_any_match_sql
from ruteo.routing_engine.types import GraphVariant, RouteContext


def od_corridor_name_parts(
    route_ctx: RouteContext,
    *,
    alias: str = "r",
) -> Dict[str, str]:
    """Fragmentos SQL para identificar el corredor directo origen–destino."""
    nom = f"TRIM(COALESCE({col_ref(alias, 'nombre')}::text, ''))"
    nom_lc = f"LOWER({nom})"
    sep = f"((LENGTH({nom}) - LENGTH(REPLACE({nom}, ' - ', ''))) / 3)"
    match_o = tokens_any_match_sql(nom_lc, route_ctx.tokens_o)
    match_d = tokens_any_match_sql(nom_lc, route_ctx.tokens_d)
    both = f"(({match_o}) AND ({match_d}))"
    direct: List[str] = []
    for to in route_ctx.tokens_o[:2]:
        for td in route_ctx.tokens_d[:2]:
            direct.append(f"{nom_lc} = '{to} - {td}'")
            direct.append(f"{nom_lc} = '{td} - {to}'")
    direct_sql = " OR ".join(direct) if direct else "FALSE"
    origin_only = f"(({match_o}) AND NOT ({match_d}) AND {sep} = 1)"
    dest_only = f"(({match_d}) AND NOT ({match_o}) AND {sep} = 1)"
    corridor_ok = f"(({direct_sql}) OR (({both}) AND {sep} = 1))"
    return {
        "nom_lc": nom_lc,
        "sep": sep,
        "match_o": match_o,
        "match_d": match_d,
        "both": both,
        "direct_sql": direct_sql,
        "origin_only": origin_only,
        "dest_only": dest_only,
        "corridor_ok": corridor_ok,
    }


def od_direct_nombre_lc_values(route_ctx: RouteContext) -> Tuple[str, ...]:
    """Nombres exactos del corredor (p. ej. chilpancingo - acapulco)."""
    names: List[str] = []
    for to in route_ctx.tokens_o[:2]:
        for td in route_ctx.tokens_d[:2]:
            names.append(f"{to} - {td}")
            names.append(f"{td} - {to}")
    return tuple(dict.fromkeys(names))


def not_toll_on_alias_sql(alias: str = "r") -> str:
    """Predicado sin peaje alineado con c_rnc (peaje, tipo_vial y nombre)."""
    peaje_q = col_ref(alias, "peaje")
    tipo_q = col_ref(alias, "tipo_vial")
    nombre_q = col_ref(alias, "nombre")
    return f"NOT ({is_toll_edge_sql(peaje_q, tipo_q, nombre_q)})"


def sin_peaje_where_sql(*, r: str = "r") -> str:
    """Subgrafo pavimentado efectivo (incluye conectores con cond_pav desconocida)."""
    es_pav = es_pavimentado_ruteo_sql(r)
    rec = f"UPPER(TRIM(COALESCE({r}.recubrimiento::text, '')))"
    return f"({es_pav}) AND {rec} NOT IN ('TIERRA', 'GRAVA')"


def od_detour_exclude_sql(
    route_ctx: RouteContext,
    *,
    alias: str = "r",
    for_access: bool = False,
) -> str:
    """Excluye sierra, rodeos y (en grafo global) tramos N/D largos fuera del corredor OD."""
    if not route_ctx.tokens_o or not route_ctx.tokens_d:
        return "TRUE"
    nom = f"TRIM(COALESCE({col_ref(alias, 'nombre')}::text, ''))"
    nom_lc = f"LOWER({nom})"
    p = od_corridor_name_parts(route_ctx, alias=alias)
    detour = detour_nombre_match_sql(nom_lc)
    sierra = sierra_nombre_match_sql(nom_lc)
    parts = [
        f"NOT ({detour})",
        f"NOT ({sierra})",
        f"NOT ({p['sep']} >= 2 AND NOT ({p['corridor_ok']}))",
    ]
    if not for_access:
        len_q = col_ref(alias, "longitud_m")
        unnamed_long = (
            f"({nom} IN ('N/D', 'N/A', '') AND COALESCE({len_q}, 0) > 2000 "
            f"AND NOT ({p['corridor_ok']}))"
        )
        parts.append(f"NOT ({unnamed_long})")
    return " AND ".join(parts)


def global_graph_extra_where(
    route_ctx: Optional[RouteContext],
    *,
    alias: str = "r",
) -> str:
    """Solo bloquea sierra/desvíos nombrados; no corta N/D del corredor."""
    if not route_ctx or not route_ctx.tokens_o or not route_ctx.tokens_d:
        return "TRUE"
    return od_detour_exclude_sql(route_ctx, alias=alias, for_access=True)


def sin_peaje_where_with_od(route_ctx: Optional[RouteContext], *, r: str = "r") -> str:
    base = sin_peaje_where_sql(r=r)
    if not route_ctx:
        return base
    extra = global_graph_extra_where(route_ctx, alias=r)
    return f"({base}) AND ({extra})"


def no_toll_where_with_od(route_ctx: Optional[RouteContext], *, alias: str = "r") -> str:
    """Sin peaje; sierra/desvíos nombrados fuera; conectividad vía costo para el resto."""
    base = not_toll_on_alias_sql(alias)
    if not route_ctx:
        return base
    extra = global_graph_extra_where(route_ctx, alias=alias)
    return f"({base}) AND ({extra})"


def access_leg_where_sql(route_ctx: RouteContext, *, alias: str = "r") -> str:
    """Acceso urbano al corredor: sin peaje ni desvíos conocidos de sierra."""
    not_toll = not_toll_on_alias_sql(alias)
    detour = od_detour_exclude_sql(route_ctx, alias=alias, for_access=True)
    return f"{not_toll} AND ({detour})"


def corridor_edge_where_sql(route_ctx: RouteContext, *, alias: str = "r") -> str:
    """Tramos del corredor OD + conectores cortos y N/D federal pavimentado."""
    p = od_corridor_name_parts(route_ctx, alias=alias)
    not_toll = not_toll_on_alias_sql(alias)
    tipo = f"TRIM(COALESCE({col_ref(alias, 'tipo_vial')}::text, ''))"
    admin = f"TRIM(COALESCE({col_ref(alias, 'administra')}::text, ''))"
    infra = is_infrastructure_connector_sql(alias)
    es_pav = es_pavimentado_ruteo_sql(alias)
    len_q = col_ref(alias, "longitud_m")
    nom = f"TRIM(COALESCE({col_ref(alias, 'nombre')}::text, ''))"
    main_vial = f"{tipo} IN ('Carretera', 'Boulevard', 'Calzada', 'Periférico')"
    federal_nd = f"""(
        {nom} IN ('N/D', 'N/A', '')
        AND COALESCE({len_q}, 0) <= 3000
        AND {tipo} = 'Carretera'
        AND {admin} = 'Federal'
        AND ({es_pav})
    )"""
    return f"""{not_toll}
        AND (
            ({main_vial} AND ({p['corridor_ok']}))
            OR (
                ({infra})
                AND COALESCE({len_q}, 0) <= {CONNECTOR_MAX_LEN_M}
            )
            OR ({federal_nd})
        )
        AND NOT ({p['origin_only']})
        AND NOT ({p['dest_only']})"""


def graph_where_for_variant(
    variant: GraphVariant,
    route_ctx: Optional[RouteContext],
    *,
    alias: str = "r",
    corridor_edge_ids: Optional[List[int]] = None,
) -> str:
    """Predicado WHERE según variante de grafo (paridad con ruteo.py)."""
    if variant == GraphVariant.TOLL_MATERIALIZED:
        return ""
    if variant == GraphVariant.SIN_PEAJE_NO_TOLL:
        return no_toll_where_with_od(route_ctx, alias=alias)
    if variant == GraphVariant.SIN_PEAJE_FULL:
        return not_toll_on_alias_sql(alias)
    if variant == GraphVariant.SIN_PEAJE_PAVED:
        return sin_peaje_where_with_od(route_ctx, r=alias)
    if variant == GraphVariant.ACCESS_PLAIN:
        return not_toll_on_alias_sql(alias)
    if variant == GraphVariant.ACCESS_NO_TOLL:
        if route_ctx:
            not_toll = not_toll_on_alias_sql(alias)
            detour = od_detour_exclude_sql(route_ctx, alias=alias, for_access=True)
            return f"({not_toll}) AND ({detour})"
        return not_toll_on_alias_sql(alias)
    if variant == GraphVariant.CORRIDOR_SUBGRAPH:
        if corridor_edge_ids:
            ids_lit = ",".join(str(int(i)) for i in corridor_edge_ids)
            return f"r.id IN ({ids_lit})"
        if route_ctx:
            return corridor_edge_where_sql(route_ctx, alias=alias)
        return "TRUE"
    if variant == GraphVariant.FALLBACK_C_RNC:
        return ""
    return ""
