"""
Motor de ruteo extensible sobre Red Nacional de Caminos (pgRouting + PostGIS).

API pública:
  - ``calcular_ruta_rnc`` — cálculo de ruta (compatible con endpoint actual)
  - ``buscar_localidades_rnc`` — catálogo de localidades
  - ``RuteoError`` — errores de negocio
  - ``RoutingOptions``, ``build_cost_sql``, ``build_graph_sql`` — extensión

Ver ``docs/RUTEA_TABLA_ROUTING.md`` y ``routing_engine/RECOMMENDED_INDEXES.sql``.
"""

from ruteo.routing_engine import scoring_config
from ruteo.routing_engine.costs import build_cost_sql
from ruteo.routing_engine.engine import calcular_ruta_rnc
from ruteo.routing_engine.errors import RuteoError
from ruteo.routing_engine.graph import build_graph_sql
from ruteo.routing_engine.localities import buscar_localidades_rnc
from ruteo.routing_engine.scoring import (
    assemble_scoring_multiplier_sql,
    factor_administracion,
    factor_carriles,
    factor_circulacion,
    factor_condicion,
    factor_peaje,
    factor_recubrimiento,
    factor_superficie,
    factor_tipo,
    factor_velocidad,
)
from ruteo.routing_engine.types import CostMode, RoutingOptions

__all__ = [
    "RuteoError",
    "RoutingOptions",
    "CostMode",
    "buscar_localidades_rnc",
    "calcular_ruta_rnc",
    "build_cost_sql",
    "build_graph_sql",
    "assemble_scoring_multiplier_sql",
    "factor_tipo",
    "factor_velocidad",
    "factor_peaje",
    "factor_superficie",
    "factor_recubrimiento",
    "factor_condicion",
    "factor_carriles",
    "factor_circulacion",
    "factor_administracion",
    "scoring_config",
]
