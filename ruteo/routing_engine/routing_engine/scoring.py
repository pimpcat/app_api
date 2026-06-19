"""
Motor de puntuación (scoring engine) — generación SQL por factor.

Cada ``factor_*`` devuelve **solo** la expresión SQL del multiplicador (o None
si la columna no existe).  ``assemble_scoring_sql`` las multiplica.

Los valores numéricos viven en ``scoring_config.py`` (único archivo editable).
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from ruteo.routing_engine import scoring_config as cfg
from ruteo.routing_engine.sql_builder import col_ref, contains_ci_sql
from ruteo.routing_engine.types import ColumnSet, CostMode, RoutingOptions

# Tipo: (columns, alias, options) -> SQL multiplicador | None
FactorFn = Callable[[ColumnSet, str, RoutingOptions], Optional[str]]

NEUTRAL = "1.0"


def _trim_text_sql(col_sql: str) -> str:
    return f"TRIM(COALESCE({col_sql}::text, ''))"


def _upper_trim_sql(col_sql: str) -> str:
    return f"UPPER(TRIM(COALESCE({col_sql}::text, '')))"


def _lookup_sql(value_expr: str, mapping: dict[str, float], default: float) -> str:
    """CASE WHEN value = key THEN factor ... ELSE default."""
    if not mapping:
        return str(default)
    parts = [f"WHEN {value_expr} = '{k}' THEN {v}" for k, v in mapping.items()]
    return f"(CASE {' '.join(parts)} ELSE {default} END)"


# ---------------------------------------------------------------------------
# Factores individuales
# ---------------------------------------------------------------------------


def factor_tipo(columns: ColumnSet, alias: str, options: RoutingOptions) -> Optional[str]:
    """
    factor_tipo — columna ``tipo_vial``.

  Penaliza vialidades urbanas y caminos frente a red principal.
    """
    if not columns.tipo_vial:
        return None
    col = _trim_text_sql(col_ref(alias, columns.tipo_vial))
    return _lookup_sql(col, cfg.FACTOR_TIPO_VIAL, cfg.FACTOR_TIPO_DEFAULT)


def factor_velocidad(columns: ColumnSet, alias: str, options: RoutingOptions) -> Optional[str]:
    """
    factor_velocidad — columnas ``velocidad_kmh`` / ``velocidad`` y ``tipo_vial``.

    En modo **distancia** devuelve 1.0 (la velocidad no altera el costo).
    En modo **tiempo**: ``BASELINE_KMH / vel_efectiva`` (vías más rápidas → menor costo).
    """
    if options.modo == CostMode.DISTANCIA:
        return None

    baseline = cfg.BASELINE_KMH
    min_spd = cfg.MIN_SPEED_KMH

    vel_col = columns.velocidad_kmh
    if vel_col:
        raw = f"NULLIF({_trim_text_sql(col_ref(alias, vel_col))}, '')::double precision"
        numeric_vel = f"NULLIF({raw}, 0)"
    else:
        numeric_vel = "NULL"

    if columns.tipo_vial:
        tipo = _trim_text_sql(col_ref(alias, columns.tipo_vial))
        tipo_lines = [
            f"WHEN {tipo} = '{k}' THEN {v}" for k, v in cfg.VELOCIDAD_ESTIMADA_POR_TIPO.items()
        ]
        tipo_case = (
            f"CASE {' '.join(tipo_lines)} ELSE {cfg.VELOCIDAD_ESTIMADA_DEFAULT} END"
        )
    else:
        tipo_case = str(cfg.VELOCIDAD_ESTIMADA_DEFAULT)

    vel_efectiva = (
        f"GREATEST(COALESCE({numeric_vel}, ({tipo_case}))::double precision, {min_spd})"
    )
    return f"({baseline} / {vel_efectiva})"


def factor_peaje(columns: ColumnSet, alias: str, options: RoutingOptions) -> Optional[str]:
    """
    factor_peaje — columna ``peaje`` (+ autopista/cuota en tipo_vial y nombre).

    Con ``evitar_peajes`` aplica ``FACTOR_PEAJE_EVITAR`` a tramos de peaje.
    """
    parts: List[str] = []

    if columns.peaje:
        col = _upper_trim_sql(col_ref(alias, columns.peaje))
        peaje_map = {k.upper(): v for k, v in cfg.FACTOR_PEAJE.items()}
        base = _lookup_sql(col, peaje_map, cfg.FACTOR_PEAJE_DEFAULT)
        parts.append(base)

    implicit_toll_parts: List[str] = []
    if columns.tipo_vial:
        tipo = _trim_text_sql(col_ref(alias, columns.tipo_vial))
        implicit_toll_parts.append(f"LOWER({tipo}) = 'autopista'")
        implicit_toll_parts.append(contains_ci_sql(tipo, "cuota"))
    if columns.nombre:
        nom = _trim_text_sql(col_ref(alias, columns.nombre))
        implicit_toll_parts.append(contains_ci_sql(nom, "autopista"))
        implicit_toll_parts.append(contains_ci_sql(nom, "cuota"))
        implicit_toll_parts.append(contains_ci_sql(nom, "peaje"))

    if implicit_toll_parts:
        cond = "(" + " OR ".join(implicit_toll_parts) + ")"
        implicit = str(cfg.FACTOR_PEAJE_AUTUPISTA_IMPLICITO)
        parts.append(f"CASE WHEN {cond} THEN {implicit} ELSE 1.0 END")

    if not parts:
        return None

    combined = parts[0] if len(parts) == 1 else f"GREATEST({' ,'.join(parts)})"

    if options.evitar_peajes:
        is_toll = f"({combined}) > 1.05"
        return f"CASE WHEN {is_toll} THEN {cfg.FACTOR_PEAJE_EVITAR} ELSE ({combined}) END"
    return f"({combined})"


def factor_superficie(columns: ColumnSet, alias: str, options: RoutingOptions) -> Optional[str]:
    """factor_superficie — columna ``cond_pav`` (superficie de rodamiento)."""
    if not columns.cond_pav:
        return None
    col = _trim_text_sql(col_ref(alias, columns.cond_pav))
    base = _lookup_sql(col, cfg.FACTOR_SUPERFICIE, cfg.FACTOR_SUPERFICIE_DEFAULT)
    if options.evitar_terracerias:
        sin_pav = f"{col} = 'Sin pavimento'"
        return f"CASE WHEN {sin_pav} THEN {cfg.TERRACERIA_EVITAR_MULTIPLIER} ELSE ({base}) END"
    return base


def factor_recubrimiento(columns: ColumnSet, alias: str, options: RoutingOptions) -> Optional[str]:
    """factor_recubrimiento — columna ``recubrimiento`` / ``recubri``."""
    col_name = columns.recubrimiento
    if not col_name:
        return None
    col = _trim_text_sql(col_ref(alias, col_name))
    base = _lookup_sql(col, cfg.FACTOR_RECUBRIMIENTO, cfg.FACTOR_RECUBRIMIENTO_DEFAULT)
    if options.evitar_terracerias:
        return (
            f"CASE WHEN {col} = 'Tierra' THEN {cfg.RECUBRIMIENTO_EVITAR_TIERRA} "
            f"WHEN {col} = 'Grava' THEN {cfg.RECUBRIMIENTO_EVITAR_GRAVA} "
            f"ELSE ({base}) END"
        )
    return base


def factor_condicion(columns: ColumnSet, alias: str, options: RoutingOptions) -> Optional[str]:
    """
    factor_condicion — columna ``condicion``.

    Solo multiplicadores; las exclusiones van a ``build_exclusion_where_sql``.
    """
    if not columns.condicion:
        return None
    col = _trim_text_sql(col_ref(alias, columns.condicion))
    mult_only = {k: v for k, v in cfg.FACTOR_CONDICION.items() if v is not None}
    return _lookup_sql(col, mult_only, cfg.FACTOR_CONDICION_DEFAULT)


def factor_carriles(columns: ColumnSet, alias: str, options: RoutingOptions) -> Optional[str]:
    """factor_carriles — columna ``carriles``."""
    if not columns.carriles:
        return None
    col = _trim_text_sql(col_ref(alias, columns.carriles))
    return _lookup_sql(col, cfg.FACTOR_CARRILES, cfg.FACTOR_CARRILES_DEFAULT)


def factor_administracion(
    columns: ColumnSet, alias: str, options: RoutingOptions
) -> Optional[str]:
    """factor_administracion — columna ``administra``."""
    if not columns.administra:
        return None
    col = _trim_text_sql(col_ref(alias, columns.administra))
    base = _lookup_sql(col, cfg.FACTOR_ADMINISTRACION, cfg.FACTOR_ADMINISTRACION_DEFAULT)

    if options.evitar_federales:
        fed = _trim_text_sql(col_ref(alias, columns.administra))
        base = (
            f"CASE WHEN {fed} = 'Federal' THEN {cfg.FACTOR_ADMIN_EVITAR_FEDERAL} "
            f"ELSE ({base}) END"
        )
    if options.evitar_estatales:
        adm = _trim_text_sql(col_ref(alias, columns.administra))
        base = (
            f"CASE WHEN {adm} = 'Estatal' THEN {cfg.FACTOR_ADMIN_EVITAR_ESTATAL} "
            f"ELSE ({base}) END"
        )
    return base


# ---------------------------------------------------------------------------
# Registro y ensamblado
# ---------------------------------------------------------------------------

def factor_circulacion(
    columns: ColumnSet, alias: str, options: RoutingOptions
) -> Optional[str]:
    """
    factor_circulacion — columna ``circulacion`` / ``circula``.

    No participa en el producto de multiplicadores.  El sentido vial se aplica
    en ``apply_circulation_forward_cost`` y ``apply_circulation_reverse_cost``
    después de ensamblar el costo puntuado.
    """
    return None


DEFAULT_SCORING_FACTORS: Sequence[FactorFn] = (
    factor_tipo,
    factor_velocidad,
    factor_peaje,
    factor_superficie,
    factor_recubrimiento,
    factor_condicion,
    factor_carriles,
    factor_administracion,
)


def assemble_scoring_multiplier_sql(
    columns: ColumnSet,
    alias: str,
    options: RoutingOptions,
    factors: Sequence[FactorFn] = DEFAULT_SCORING_FACTORS,
) -> str:
    """
    Multiplica todos los factores activos.

    Retorna ``1.0`` si ningún factor aplica (p. ej. modo distancia sin columnas).
    """
    parts: List[str] = []
    for fn in factors:
        expr = fn(columns, alias, options)
        if expr:
            parts.append(f"({expr})")
    if not parts:
        return NEUTRAL
    return " * ".join(parts)


def build_exclusion_where_sql(
    columns: ColumnSet,
    alias: str,
    options: RoutingOptions,
) -> Optional[str]:
    """
    Predicados WHERE por condición / estatus no transitable.

    Respeta ``options.evitar_construccion`` (excluye cerrado, planeado, deshabilitado).
    """
    clauses: List[str] = []

    if options.evitar_construccion and columns.condicion:
        col = _trim_text_sql(col_ref(alias, columns.condicion))
        excluded = [k for k, v in cfg.FACTOR_CONDICION.items() if v is None]
        if excluded:
            vals = ", ".join(f"'{v}'" for v in excluded)
            clauses.append(f"{col} NOT IN ({vals})")

    estatus_col = columns.estatus
    if options.evitar_construccion and estatus_col:
        col = _trim_text_sql(col_ref(alias, estatus_col))
        vals = ", ".join(f"'{v}'" for v in cfg.EXCLUIR_ESTATUS)
        clauses.append(f"{col} NOT IN ({vals})")

    if not clauses:
        return None
    return " AND ".join(clauses)


def apply_circulation_forward_cost(
    cost_expr: str,
    columns: ColumnSet,
    alias: str,
    options: RoutingOptions,
) -> str:
    """
    factor_circulacion (sentido) — ajuste en ``cost`` hacia adelante.

    Cerrada en ambos sentidos → cost = -1 (impasable en pgRouting).
    """
    if not options.respetar_sentido or not columns.circulacion:
        return cost_expr
    circ = _trim_text_sql(col_ref(alias, columns.circulacion))
    imp = cfg.COSTO_IMPASABLE
    return f"CASE WHEN {circ} = '{cfg.CIRCULACION_CERRADA}' THEN {imp} ELSE ({cost_expr}) END"


def apply_circulation_reverse_cost(
    cost_expr: str,
    columns: ColumnSet,
    alias: str,
    options: RoutingOptions,
) -> str:
    """
    factor_circulacion — ``reverse_cost``.

    - Dos sentidos: reverse_cost = cost
    - Un sentido: reverse_cost = -1
    - Cerrada: reverse_cost = -1
    """
    if not options.respetar_sentido or not columns.circulacion:
        return cost_expr
    circ = _trim_text_sql(col_ref(alias, columns.circulacion))
    imp = cfg.COSTO_IMPASABLE
    uno = cfg.CIRCULACION_UN_SENTIDO
    cerr = cfg.CIRCULACION_CERRADA
    return f"""CASE
        WHEN {circ} = '{cerr}' THEN {imp}
        WHEN {circ} = '{uno}' THEN {imp}
        ELSE ({cost_expr})
    END"""
