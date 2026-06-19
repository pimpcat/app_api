"""
Paquete de ruteo RNC (Red Nacional de Caminos).
"""

from ruteo.facade import *  # noqa: F403
from ruteo.facade import (
    RuteoError,
    buscar_localidades_rnc,
    calcular_ruta_rnc,
)

__all__ = [
    "RuteoError",
    "buscar_localidades_rnc",
    "calcular_ruta_rnc",
]
