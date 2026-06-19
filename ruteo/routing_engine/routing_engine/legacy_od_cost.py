"""
Costos SQL legacy sin peajes y por distancia OD (paridad con ruteo.py).

Expresiones puras (sin I/O); usan nombres de columna fijos de ``c_rnc_routing``.
"""

from __future__ import annotations

from typing import Dict, Optional

from ruteo.routing_engine.restrictions import (
    CONNECTOR_MAX_LEN_M,
    INFRASTRUCTURE_TIPOS,
    is_toll_edge_sql,
)
from ruteo.routing_engine.sql_builder import col_ref, tokens_any_match_sql
from ruteo.routing_engine.types import RouteContext

# Penalizaciones en metros equivalentes (escala ~100–150 km por ruta estatal).
PENALTY_TOLL_EDGE_M = 600_000.0
PENALTY_UNPAVED_M = 120_000.0
PENALTY_DIRT_SURFACE_M = 150_000.0
PENALTY_MINOR_ROAD_M = 60_000.0
PENALTY_URBAN_STREET_M = 30_000.0
JERARQUIA_ADMIN_FACTOR_M = 800.0
JERARQUIA_TIPO_FACTOR_M = 250.0
BASELINE_KMH = 90.0
FEDERAL_CARRETERA_FACTOR = 0.82
DIRECT_FEDERAL_NAMED_FACTOR = 0.58
OD_DIRECT_CORRIDOR_FACTOR = 0.32
OD_BOTH_TOKENS_FACTOR = 0.40
PENALTY_ORIGIN_SCENIC_DETOUR_M = 200_000.0
PENALTY_OFF_CORRIDOR_SCENIC_M = 250_000.0
PENALTY_SCENIC_CORRIDOR_M = 180_000.0
PENALTY_SIERRA_FRAGMENT_M = 450_000.0
PENALTY_DETOUR_NAMED_M = 500_000.0
PENALTY_UNNAMED_BASE_M = 80_000.0
PENALTY_UNNAMED_PER_M = 150.0

SIERRA_NOMBRE_FRAGMENTS = (
    "omiltemi",
    "jaleaca",
    "amojileca",
    "tlahuizapa",
    "zoyatepec",
    "coacoyulillo",
    "ocotito",
    "coapango",
)
DETOUR_NOMBRE_FRAGMENTS = (
    "quechultenango",
    "petaquillas",
    "grutas",
)

MINOR_ROAD_TIPOS = (
    "Camino",
    "Callejón",
    "Vereda",
    "Andador",
    "Peatonal",
)
URBAN_STREET_TIPOS = (
    "Calle",
    "Privada",
    "Cerrada",
    "Diagonal",
    "Prolongación",
    "Continuación",
    "Ampliación",
)


def sierra_nombre_match_sql(nom_lc: str) -> str:
    if not SIERRA_NOMBRE_FRAGMENTS:
        return "FALSE"
    return "(" + " OR ".join(
        f"strpos({nom_lc}, '{frag}') > 0" for frag in SIERRA_NOMBRE_FRAGMENTS
    ) + ")"


def detour_nombre_match_sql(nom_lc: str) -> str:
    frags = SIERRA_NOMBRE_FRAGMENTS + DETOUR_NOMBRE_FRAGMENTS
    if not frags:
        return "FALSE"
    return "(" + " OR ".join(
        f"strpos({nom_lc}, '{frag}') > 0" for frag in frags
    ) + ")"


def od_corredor_adjustments_sql(alias: str, ctx: RouteContext) -> Dict[str, str]:
    """Preferencia automática de corredor según el par origen–destino."""
    nom = f"TRIM(COALESCE({col_ref(alias, 'nombre')}::text, ''))"
    nom_lc = f"LOWER({nom})"
    sep = f"((LENGTH({nom}) - LENGTH(REPLACE({nom}, ' - ', ''))) / 3)"
    tipo_q = col_ref(alias, "tipo_vial")
    match_o = tokens_any_match_sql(nom_lc, ctx.tokens_o)
    match_d = tokens_any_match_sql(nom_lc, ctx.tokens_d)
    both = f"(({match_o}) AND ({match_d}))"
    main_vial = (
        f"TRIM(COALESCE({tipo_q}::text, '')) IN "
        f"('Carretera', 'Autopista', 'Boulevard', 'Periférico', 'Calzada')"
    )

    corridor_cases: list[str] = []
    for to in ctx.tokens_o[:2]:
        for td in ctx.tokens_d[:2]:
            corridor_cases.append(
                f"WHEN {nom_lc} = '{to} - {td}' THEN {OD_DIRECT_CORRIDOR_FACTOR}"
            )
            corridor_cases.append(
                f"WHEN {nom_lc} = '{td} - {to}' THEN {OD_DIRECT_CORRIDOR_FACTOR}"
            )
    corridor_sql = "\n            ".join(corridor_cases)

    factor = f"""
        * CASE
            {corridor_sql}
            WHEN ({both}) AND {main_vial} THEN {OD_BOTH_TOKENS_FACTOR}
            ELSE 1.0
          END
    """.strip()

    penalties = f"""
        + CASE WHEN ({match_o}) AND NOT ({match_d}) AND {sep} >= 1
             AND TRIM(COALESCE({tipo_q}::text, '')) = 'Carretera'
            THEN {PENALTY_ORIGIN_SCENIC_DETOUR_M} ELSE 0.0 END
        + CASE WHEN ({match_d}) AND NOT ({match_o}) AND {sep} = 1
             AND TRIM(COALESCE({tipo_q}::text, '')) = 'Carretera'
            THEN {PENALTY_OFF_CORRIDOR_SCENIC_M} ELSE 0.0 END
        + CASE WHEN NOT ({both}) AND NOT ({match_o}) AND NOT ({match_d}) AND {sep} >= 2
             AND TRIM(COALESCE({tipo_q}::text, '')) = 'Carretera'
            THEN {PENALTY_OFF_CORRIDOR_SCENIC_M} ELSE 0.0 END
    """.strip()

    return {"factor": factor, "penalties": penalties}


def is_infrastructure_connector_sql(alias: str = "r") -> str:
    """Enlace, glorieta, retorno, etc. (conectores topológicos de la red)."""
    tipos = ", ".join(f"'{t}'" for t in INFRASTRUCTURE_TIPOS)
    col = col_ref(alias, "tipo_vial")
    return f"TRIM(COALESCE({col}::text, '')) IN ({tipos})"


def es_pavimentado_ruteo_sql(alias: str = "r") -> str:
    """
    Pavimento efectivo para ruteo: enlaces/glorietas con cond_pav N/D o N/A
    no deben recibir penalización de terracería si no son tierra/grava.
    """
    connector = is_infrastructure_connector_sql(alias)
    cond = f"UPPER(TRIM(COALESCE({col_ref(alias, 'cond_pav')}::text, '')))"
    rec = f"UPPER(TRIM(COALESCE({col_ref(alias, 'recubrimiento')}::text, '')))"
    max_len = CONNECTOR_MAX_LEN_M
    return f"""(
        {col_ref(alias, "es_pavimentado")}
        OR (
            {connector}
            AND {cond} IN ('N/D', 'N/A', '')
            AND {rec} NOT IN ('TIERRA', 'GRAVA')
            AND COALESCE({col_ref(alias, "longitud_m")}, 0) <= {max_len}
        )
    )"""


def tipo_in_list_sql(alias: str, col: str, tipos: tuple[str, ...]) -> str:
    lista = ", ".join(f"'{t}'" for t in tipos)
    return f"TRIM(COALESCE({col_ref(alias, col)}::text, '')) IN ({lista})"


def velocidad_efectiva_kmh_sql(alias: str = "r") -> str:
    """km/h estimados: dato RNC o inferidos por jerarquía de tipo vial."""
    jtv = col_ref(alias, "jerarquia_tipo_vial")
    vk = col_ref(alias, "velocidad_kmh")
    return f"""GREATEST(
        COALESCE(
            NULLIF({vk}, 0),
            CASE {jtv}
                WHEN 1 THEN 100
                WHEN 2 THEN 80
                WHEN 3 THEN 70
                WHEN 4 THEN 60
                WHEN 5 THEN 60
                WHEN 6 THEN 55
                WHEN 7 THEN 55
                WHEN 8 THEN 55
                WHEN 9 THEN 55
                WHEN 10 THEN 55
                WHEN 11 THEN 50
                WHEN 12 THEN 45
                WHEN 13 THEN 40
                WHEN 14 THEN 40
                ELSE 35
            END
        )::double precision,
        20.0
    )"""


def tiempo_base_cost_sql(alias: str = "r") -> str:
    """Costo ∝ tiempo de recorrido (normalizado a BASELINE_KMH km/h)."""
    vel = velocidad_efectiva_kmh_sql(alias)
    base = BASELINE_KMH
    return f"({col_ref(alias, 'longitud_m')} * {base} / {vel})"


def nombre_corredor_sql(alias: str) -> Dict[str, str]:
    """Ajustes por nombre: prioriza corredores directos y penaliza sierra / sin nombre."""
    nom = f"TRIM(COALESCE({col_ref(alias, 'nombre')}::text, ''))"
    nom_lc = f"LOWER({nom})"
    sep = f"((LENGTH({nom}) - LENGTH(REPLACE({nom}, ' - ', ''))) / 3)"
    tipo_q = col_ref(alias, "tipo_vial")
    admin_q = col_ref(alias, "administra")
    es_pav = es_pavimentado_ruteo_sql(alias)
    len_q = col_ref(alias, "longitud_m")
    sierra_or = sierra_nombre_match_sql(nom_lc)
    detour_or = detour_nombre_match_sql(nom_lc)

    factor = f"""
        * CASE
            WHEN {sep} = 1
             AND TRIM(COALESCE({tipo_q}::text, '')) = 'Carretera'
             AND TRIM(COALESCE({admin_q}::text, '')) = 'Federal'
             AND ({es_pav})
            THEN {DIRECT_FEDERAL_NAMED_FACTOR}
            ELSE 1.0
          END
    """.strip()

    penalties = f"""
        + CASE WHEN {sep} >= 2
             AND TRIM(COALESCE({tipo_q}::text, '')) = 'Carretera'
            THEN {PENALTY_SCENIC_CORRIDOR_M} ELSE 0.0 END
        + CASE WHEN ({sierra_or}) THEN {PENALTY_SIERRA_FRAGMENT_M} ELSE 0.0 END
        + CASE WHEN ({detour_or}) THEN {PENALTY_DETOUR_NAMED_M} ELSE 0.0 END
        + CASE WHEN {nom} IN ('N/D', 'N/A', '')
             AND {len_q} > 200
            THEN {PENALTY_UNNAMED_BASE_M} + {len_q} * {PENALTY_UNNAMED_PER_M}
            ELSE 0.0 END
    """.strip()

    return {"factor": factor, "penalties": penalties}


def sin_peaje_cost_sql(
    *, alias: str = "r", route_ctx: Optional[RouteContext] = None
) -> str:
    """Costo sin peajes: tiempo + jerarquía + corredor (sin JOIN a c_rnc)."""
    t = PENALTY_TOLL_EDGE_M
    u = PENALTY_UNPAVED_M
    d = PENALTY_DIRT_SURFACE_M
    m = PENALTY_MINOR_ROAD_M
    s = PENALTY_URBAN_STREET_M
    ja = JERARQUIA_ADMIN_FACTOR_M
    jt = JERARQUIA_TIPO_FACTOR_M
    f = FEDERAL_CARRETERA_FACTOR
    es_pav = es_pavimentado_ruteo_sql(alias)
    peaje_q = col_ref(alias, "peaje")
    tipo_q = col_ref(alias, "tipo_vial")
    nombre_q = col_ref(alias, "nombre")
    is_toll = is_toll_edge_sql(peaje_q, tipo_q, nombre_q)
    tiempo = tiempo_base_cost_sql(alias)
    minor = tipo_in_list_sql(alias, "tipo_vial", MINOR_ROAD_TIPOS)
    urban = tipo_in_list_sql(alias, "tipo_vial", URBAN_STREET_TIPOS)
    rec = f"TRIM(COALESCE({col_ref(alias, 'recubrimiento')}::text, ''))"
    corredor = nombre_corredor_sql(alias)
    od = od_corredor_adjustments_sql(alias, route_ctx) if route_ctx else {"factor": "", "penalties": ""}
    nom_lc_detour = f"LOWER(TRIM(COALESCE({col_ref(alias, 'nombre')}::text, '')))"
    detour_pen = (
        f"+ CASE WHEN ({detour_nombre_match_sql(nom_lc_detour)}) "
        f"THEN {PENALTY_DETOUR_NAMED_M} ELSE 0.0 END"
    )
    return f"""
        GREATEST(
            ({tiempo})
            {od["factor"]}
            * CASE
                WHEN TRIM(COALESCE({tipo_q}::text, '')) = 'Carretera'
                 AND TRIM(COALESCE({col_ref(alias, 'administra')}::text, '')) = 'Federal'
                 AND ({es_pav})
                THEN {f}
                ELSE 1.0
              END
            {corredor["factor"]}
            * (1.0 + ({col_ref(alias, 'jerarquia_tipo_vial')} - 1) * 0.08)
            * (1.0 + ({col_ref(alias, 'jerarquia_admin')} - 1) * 0.04)
            + CASE WHEN ({is_toll}) THEN {t} ELSE 0.0 END
            + CASE WHEN NOT ({es_pav}) THEN {u} ELSE 0.0 END
            + CASE WHEN {rec} IN ('Tierra', 'Grava') THEN {d} ELSE 0.0 END
            + CASE WHEN ({minor}) THEN {m} ELSE 0.0 END
            + CASE WHEN ({urban}) THEN {s} ELSE 0.0 END
            {od["penalties"]}
            {corredor["penalties"]}
            {detour_pen}
            + ({col_ref(alias, 'jerarquia_admin')} - 1) * {ja}
            + ({col_ref(alias, 'jerarquia_tipo_vial')} - 1) * {jt},
            1.0
        )
    """.strip()


def distance_no_toll_cost_sql(
    *, alias: str = "r", route_ctx: Optional[RouteContext] = None
) -> str:
    """Costo ≈ distancia con fuerte rechazo a sierra / terracería / sin nombre."""
    es_pav = es_pavimentado_ruteo_sql(alias)
    len_q = col_ref(alias, "longitud_m")
    nom = f"TRIM(COALESCE({col_ref(alias, 'nombre')}::text, ''))"
    nom_lc = f"LOWER({nom})"
    sierra = sierra_nombre_match_sql(nom_lc)
    detour = detour_nombre_match_sql(nom_lc)
    od_factor = ""
    if route_ctx and route_ctx.tokens_o and route_ctx.tokens_d:
        to = route_ctx.tokens_o[0]
        td = route_ctx.tokens_d[0]
        match_o = tokens_any_match_sql(nom_lc, route_ctx.tokens_o)
        match_d = tokens_any_match_sql(nom_lc, route_ctx.tokens_d)
        both = f"(({match_o}) AND ({match_d}))"
        od_factor = f"""
            * CASE
                WHEN {nom_lc} IN ('{to} - {td}', '{td} - {to}') THEN 0.45
                WHEN ({both}) THEN 0.65
                ELSE 1.0
              END
        """.strip()
    return (
        f"GREATEST({len_q} "
        f"* CASE WHEN ({es_pav}) THEN 1.0 ELSE 25.0 END "
        f"* CASE WHEN ({sierra}) THEN 80.0 ELSE 1.0 END "
        f"* CASE WHEN ({detour}) THEN 120.0 ELSE 1.0 END "
        f"* CASE WHEN {nom} IN ('N/D', 'N/A', '') AND {len_q} > 400 "
        f"THEN (1.0 + {len_q} / 150.0) ELSE 1.0 END "
        f"{od_factor}, 1.0)"
    )
