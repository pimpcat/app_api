"""
Utilidades de construcción SQL reutilizables.

Funciones puras (sin I/O) para expresiones PostGIS y referencias a columnas.
"""

from __future__ import annotations

from utils import quote_ident

WGS84 = 4326


def col_ref(alias: str, col: str) -> str:
    """Referencia calificada ``alias.col`` o solo ``col`` si alias vacío."""
    return f"{alias}.{col}" if alias else col


def contains_ci_sql(expr: str, needle: str) -> str:
    """Subcadena case-insensitive sin ``%`` (psycopg 3 rechaza % en SQL embebido)."""
    safe = needle.lower().replace("'", "''")
    return f"strpos(LOWER({expr}), '{safe}') > 0"


def len_geog_m_sql(geom_expr: str) -> str:
    """Longitud en metros (WGS84 geográfico; soporta geometrías proyectadas)."""
    return f"ST_Length(ST_Transform({geom_expr}, {WGS84})::geography)"


def dist_geog_m_sql(geom_a: str, geom_b: str) -> str:
    """Distancia en metros entre dos geometrías (cualquier SRID de entrada)."""
    return (
        f"ST_Distance(ST_Transform({geom_a}, {WGS84})::geography, "
        f"ST_Transform({geom_b}, {WGS84})::geography)"
    )


def dwithin_geog_m_sql(geom_a: str, geom_b: str, max_m: float) -> str:
    """Predicado ST_DWithin en metros sobre geography."""
    return (
        f"ST_DWithin(ST_Transform({geom_a}, {WGS84})::geography, "
        f"ST_Transform({geom_b}, {WGS84})::geography, {max_m})"
    )


def tipo_in_list_sql(alias: str, col: str, tipos: tuple[str, ...]) -> str:
    """``tipo_vial IN (...)`` con TRIM/COALESCE."""
    lista = ", ".join(f"'{t}'" for t in tipos)
    return f"TRIM(COALESCE({col_ref(alias, col)}::text, '')) IN ({lista})"


def tokens_any_match_sql(nom_lc: str, tokens: tuple[str, ...]) -> str:
    """Verdadero si algún token aparece en ``nom_lc`` (ya en minúsculas)."""
    if not tokens:
        return "FALSE"
    return "(" + " OR ".join(f"strpos({nom_lc}, '{t}') > 0" for t in tokens) + ")"


def q_ident(name: str) -> str:
    """Alias de ``quote_ident`` para consistencia interna del paquete."""
    return quote_ident(name)
