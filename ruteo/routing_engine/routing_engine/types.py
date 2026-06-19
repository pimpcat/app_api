"""
Tipos de datos del motor de ruteo.

``RoutingOptions`` centraliza las restricciones y el modo de costo.
Nuevas restricciones (evitar_federales, solo_autopistas, etc.) se añaden
como campos opcionales o mediante el registro en ``restrictions.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class CostMode(str, Enum):
    """Modo base de cálculo de costo por arista."""

    DISTANCIA = "distancia"
    TIEMPO = "tiempo"


class CostProfile(str, Enum):
    """
    Perfil de costo para ``build_cost_sql``.

    - ``SCORING``: motor multiplicativo (``scoring_config.py``).
    - ``LEGACY_SIN_PEAJE``: costo histórico sin peajes (paridad endpoint).
    - ``DISTANCE_OD``: costo por distancia penalizada (stitch / acceso OD).
    - ``MATERIALIZED``: costos precalculados en ``c_rnc_routing`` (con peajes).
    """

    SCORING = "scoring"
    LEGACY_SIN_PEAJE = "legacy_sin_peaje"
    DISTANCE_OD = "distance_od"
    MATERIALIZED = "materialized"


class GraphVariant(str, Enum):
    """Variante de grafo (filtros WHERE) para ``build_graph_sql``."""

    TOLL_MATERIALIZED = "toll_materialized"
    SIN_PEAJE_NO_TOLL = "sin_peaje_no_toll"
    SIN_PEAJE_FULL = "sin_peaje_full"
    SIN_PEAJE_PAVED = "sin_peaje_paved"
    ACCESS_PLAIN = "access_plain"
    ACCESS_NO_TOLL = "access_no_toll"
    CORRIDOR_SUBGRAPH = "corridor_subgraph"
    FALLBACK_C_RNC = "fallback_c_rnc"


@dataclass(frozen=True)
class RouteContext:
    """
    Contexto del par origen–destino para ajustes dinámicos de costo.

    Usado por la estrategia legacy «sin peajes» (corredor OD, stitch).
    """

    origen_nombre: str
    destino_nombre: str
    tokens_o: Tuple[str, ...]
    tokens_d: Tuple[str, ...]


@dataclass
class ColumnSet:
    """
    Columnas resueltas de ``c_rnc`` / ``c_rnc_routing`` para una conexión.

    Se construye una vez por petición (o desde caché de esquema) y se pasa
    a ``build_cost_sql`` / ``build_graph_sql`` para evitar consultas repetidas
  a ``information_schema``.
    """

    edge_id: str
    source: str
    target: str
    geom: str
    longitud_m: Optional[str] = None
    velocidad_kmh: Optional[str] = None
    peaje: Optional[str] = None
    tipo_vial: Optional[str] = None
    nombre: Optional[str] = None
    cond_pav: Optional[str] = None
    recubrimiento: Optional[str] = None
    condicion: Optional[str] = None
    circulacion: Optional[str] = None
    es_pavimentado: Optional[str] = None
    jerarquia_tipo_vial: Optional[str] = None
    jerarquia_admin: Optional[str] = None
    administra: Optional[str] = None
    carriles: Optional[str] = None
    estatus: Optional[str] = None
    cost_col: Optional[str] = None
    reverse_cost_col: Optional[str] = None

    @classmethod
    def from_route_meta(cls, route_meta: Dict[str, Any]) -> "ColumnSet":
        """Reconstruye desde el dict de metadatos de red (``schema.routing_meta``)."""
        cols = route_meta.get("columns") or {}
        return cls(
            edge_id=route_meta.get("edge_id") or "gid",
            source=route_meta.get("source") or "source",
            target=route_meta.get("target") or "target",
            geom=route_meta.get("geom") or "the_geom",
            **{k: cols.get(k) for k in (
                "longitud_m", "velocidad_kmh", "peaje", "tipo_vial", "nombre",
                "cond_pav", "recubrimiento", "condicion", "circulacion",
                "es_pavimentado", "jerarquia_tipo_vial", "jerarquia_admin",
                "administra", "carriles", "estatus", "cost_col", "reverse_cost_col",
            )},
        )


@dataclass
class RoutingOptions:
    """
    Opciones de ruteo para construcción dinámica de costo y grafo.

    ``from_usar_peajes`` mapea el parámetro legacy del endpoint actual.
    """

    modo: CostMode = CostMode.TIEMPO
    evitar_peajes: bool = False
    evitar_terracerias: bool = False
    evitar_construccion: bool = True
    respetar_sentido: bool = True
    usar_tabla_routing: bool = True
    usar_costos_materializados: bool = False
    cost_profile: CostProfile = CostProfile.SCORING
    graph_variant: GraphVariant = GraphVariant.TOLL_MATERIALIZED
    # Extensiones futuras (no activas por defecto):
    evitar_federales: bool = False
    evitar_estatales: bool = False
    solo_pavimento: bool = False
    solo_autopistas: bool = False
    vehiculo_pesado: bool = False
    # Ajustes legacy sin-peajes (corredor OD); se ignoran si evitar_peajes=False
    route_context: Optional[RouteContext] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_usar_peajes(cls, usar_peajes: bool, route_ctx: Optional[RouteContext] = None) -> "RoutingOptions":
        """Compatibilidad con ``calcular_ruta_rnc(..., usar_peajes=...)``."""
        if usar_peajes:
            return cls(
                modo=CostMode.TIEMPO,
                evitar_peajes=False,
                evitar_terracerias=False,
                evitar_construccion=False,
                usar_costos_materializados=True,
                cost_profile=CostProfile.MATERIALIZED,
                graph_variant=GraphVariant.TOLL_MATERIALIZED,
            )
        return cls(
            modo=CostMode.TIEMPO,
            evitar_peajes=True,
            evitar_terracerias=True,
            evitar_construccion=True,
            usar_costos_materializados=False,
            cost_profile=CostProfile.LEGACY_SIN_PEAJE,
            graph_variant=GraphVariant.SIN_PEAJE_NO_TOLL,
            route_context=route_ctx,
        )


@dataclass(frozen=True)
class CostSql:
    """Expresiones SQL para ``cost`` y ``reverse_cost`` de pgRouting."""

    cost: str
    reverse_cost: str
