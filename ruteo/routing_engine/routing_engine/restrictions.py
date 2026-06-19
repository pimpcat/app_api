"""
Registro extensible de restricciones de ruteo.

Cada restricción puede aportar:
  - ``where_clause``: filtra aristas del grafo (exclusión dura).
  - ``cost_penalty_sql``: penalización aditiva al costo base.
  - ``cost_multiplier_sql``: factor multiplicativo al costo base.

Para añadir ``evitar_federales``, ``solo_autopistas``, etc., registrar una nueva
clase que implemente ``Restriction`` y añadirla a ``DEFAULT_RESTRICTIONS``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ruteo.routing_engine.sql_builder import col_ref, contains_ci_sql
from ruteo.routing_engine.types import ColumnSet, RoutingOptions

# Penalizaciones en metros equivalentes (escala ~100–150 km por ruta estatal).
PENALTY_TOLL_EDGE_M = 600_000.0
PENALTY_UNPAVED_M = 120_000.0
PENALTY_DIRT_SURFACE_M = 150_000.0

CONNECTOR_MAX_LEN_M = 350.0
INFRASTRUCTURE_TIPOS = (
    "Enlace",
    "Glorieta",
    "Retorno",
    "Retorno U",
    "Rampa de frenado",
    "Viaducto",
    "Corredor",
    "Circunvalación",
    "Circuito",
)

CONSTRUCTION_VALUES = ("En construcción", "Planeado", "Deshabilitado")
DIRT_RECUBRIMIENTO = ("TIERRA", "GRAVA", "Tierra", "Grava")
UNPAVED_COND_PAV = ("Sin pavimento", "SIN PAVIMENTO")


class Restriction(ABC):
    """Contrato para una restricción de ruteo."""

    name: str

    @abstractmethod
    def active(self, options: RoutingOptions) -> bool:
        """True si la restricción aplica según ``RoutingOptions``."""

    def where_clause(self, columns: ColumnSet, alias: str = "r") -> Optional[str]:
        """Predicado SQL para excluir aristas (None = sin filtro)."""
        return None

    def cost_penalty_sql(self, columns: ColumnSet, alias: str = "r") -> Optional[str]:
        """Término SQL aditivo ``+ CASE ... END`` (None = sin penalización)."""
        return None

    def cost_multiplier_sql(self, columns: ColumnSet, alias: str = "r") -> Optional[str]:
        """Factor multiplicativo (None = 1.0)."""
        return None


def is_toll_edge_sql(
    peaje_q: str,
    tipo_vial_q: Optional[str] = None,
    nombre_q: Optional[str] = None,
) -> str:
    """Expresión SQL verdadera cuando la arista es de peaje (catálogo c_rnc)."""
    parts = [f"UPPER(TRIM(COALESCE({peaje_q}::text, 'No'))) IN ('SI', 'SÍ')"]
    if tipo_vial_q:
        tipo_expr = f"TRIM(COALESCE({tipo_vial_q}::text, ''))"
        parts.append(f"LOWER({tipo_expr}) = 'autopista'")
        parts.append(contains_ci_sql(tipo_expr, "Cuota"))
    if nombre_q:
        nom_expr = f"TRIM(COALESCE({nombre_q}::text, ''))"
        parts.append(contains_ci_sql(nom_expr, "autopista"))
        parts.append(contains_ci_sql(nom_expr, "cuota"))
        parts.append(contains_ci_sql(nom_expr, "peaje"))
    return "(" + " OR ".join(parts) + ")"


def is_infrastructure_connector_sql(columns: ColumnSet, alias: str = "r") -> str:
    """Enlace, glorieta, retorno, etc."""
    if not columns.tipo_vial:
        return "FALSE"
    tipos = ", ".join(f"'{t}'" for t in INFRASTRUCTURE_TIPOS)
    col = col_ref(alias, columns.tipo_vial)
    return f"TRIM(COALESCE({col}::text, '')) IN ({tipos})"


def effective_paved_sql(columns: ColumnSet, alias: str = "r") -> str:
    """
    Pavimento efectivo: conectores con cond_pav N/D no se penalizan
    si no son tierra/grava y son cortos.
    """
    if columns.es_pavimentado:
        base = col_ref(alias, columns.es_pavimentado)
        paved = f"({base})"
    else:
        paved = "FALSE"

    if not columns.cond_pav and not columns.recubrimiento:
        return paved

    connector = is_infrastructure_connector_sql(columns, alias)
    cond = (
        f"UPPER(TRIM(COALESCE({col_ref(alias, columns.cond_pav)}::text, '')))"
        if columns.cond_pav
        else "''"
    )
    rec = (
        f"UPPER(TRIM(COALESCE({col_ref(alias, columns.recubrimiento)}::text, '')))"
        if columns.recubrimiento
        else "''"
    )
    len_expr = (
        f"COALESCE({col_ref(alias, columns.longitud_m)}, 0)"
        if columns.longitud_m
        else "0"
    )
    return f"""(
        {paved}
        OR (
            {connector}
            AND {cond} IN ('N/D', 'N/A', '')
            AND {rec} NOT IN ('TIERRA', 'GRAVA')
            AND {len_expr} <= {CONNECTOR_MAX_LEN_M}
        )
    )"""


@dataclass
class AvoidConstructionRestriction(Restriction):
    """Delegado al scoring engine (``build_exclusion_where_sql``)."""

    name: str = "evitar_construccion"

    def active(self, options: RoutingOptions) -> bool:
        return False  # exclusiones en scoring.build_exclusion_where_sql


@dataclass
class AvoidTollsRestriction(Restriction):
    """Delegado al scoring (factor_peaje) y WHERE en graph.build_graph_sql."""

    name: str = "evitar_peajes"

    def active(self, options: RoutingOptions) -> bool:
        return False


@dataclass
class AvoidUnpavedRestriction(Restriction):
    """Delegado al scoring (factor_superficie / factor_recubrimiento)."""

    name: str = "evitar_terracerias"

    def active(self, options: RoutingOptions) -> bool:
        return False


@dataclass
class SoloPavimentoRestriction(Restriction):
    """Restricción futura: subgrafo solo pavimentado."""

    name: str = "solo_pavimento"

    def active(self, options: RoutingOptions) -> bool:
        return options.solo_pavimento

    def where_clause(self, columns: ColumnSet, alias: str = "r") -> Optional[str]:
        paved = effective_paved_sql(columns, alias)
        if columns.recubrimiento:
            rec = f"UPPER(TRIM(COALESCE({col_ref(alias, columns.recubrimiento)}::text, '')))"
            return f"({paved}) AND {rec} NOT IN ('TIERRA', 'GRAVA')"
        return f"({paved})"


@dataclass
class AvoidFederalesRestriction(Restriction):
    name: str = "evitar_federales"

    def active(self, options: RoutingOptions) -> bool:
        return options.evitar_federales

    def where_clause(self, columns: ColumnSet, alias: str = "r") -> Optional[str]:
        if not columns.administra:
            return None
        col = col_ref(alias, columns.administra)
        return f"TRIM(COALESCE({col}::text, '')) <> 'Federal'"


@dataclass
class SoloAutopistasRestriction(Restriction):
    name: str = "solo_autopistas"

    def active(self, options: RoutingOptions) -> bool:
        return options.solo_autopistas

    def where_clause(self, columns: ColumnSet, alias: str = "r") -> Optional[str]:
        if not columns.tipo_vial:
            return None
        col = col_ref(alias, columns.tipo_vial)
        return f"LOWER(TRIM(COALESCE({col}::text, ''))) = 'autopista'"


DEFAULT_RESTRICTIONS: Sequence[Restriction] = (
    AvoidConstructionRestriction(),
    AvoidTollsRestriction(),
    AvoidUnpavedRestriction(),
    SoloPavimentoRestriction(),
    AvoidFederalesRestriction(),
    SoloAutopistasRestriction(),
)


def build_where_sql(
    options: RoutingOptions,
    columns: ColumnSet,
    *,
    alias: str = "r",
    restrictions: Sequence[Restriction] = DEFAULT_RESTRICTIONS,
    extra_where: Optional[str] = None,
) -> str:
    """
    Combina predicados WHERE de todas las restricciones activas.

    Retorna cadena vacía si no hay filtros.
    """
    clauses: List[str] = []
    for r in restrictions:
        if not r.active(options):
            continue
        w = r.where_clause(columns, alias)
        if w:
            clauses.append(f"({w})")
    if extra_where:
        clauses.append(f"({extra_where})")
    return " AND ".join(clauses)


def build_cost_adjustments_sql(
    options: RoutingOptions,
    columns: ColumnSet,
    *,
    alias: str = "r",
    restrictions: Sequence[Restriction] = DEFAULT_RESTRICTIONS,
) -> str:
    """
    Suma penalizaciones y aplica multiplicadores de restricciones activas.

    Retorna ``0.0`` o expresión ``+ ...`` lista para insertar en GREATEST(...).
    """
    penalties: List[str] = []
    multipliers: List[str] = ["1.0"]
    for r in restrictions:
        if not r.active(options):
            continue
        p = r.cost_penalty_sql(columns, alias)
        if p:
            penalties.append(f"({p})")
        m = r.cost_multiplier_sql(columns, alias)
        if m:
            multipliers.append(f"({m})")
    penalty_sum = " + ".join(penalties) if penalties else "0.0"
    if not penalties and len(multipliers) == 1:
        return "0.0"
    if penalties:
        return penalty_sum
    mult = " * ".join(multipliers)
    return f"0.0"  # multiplicadores se aplican en costs.py si se necesitan en el futuro
