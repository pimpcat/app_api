"""
Caché de metadatos de esquema y columnas.

Evita recomponer diccionarios de columnas y repetir consultas a
``information_schema`` en cada petición HTTP.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict

from database import connect

# Incrementar al cambiar contrato de grafos / restricciones (invalida lru_cache).
SCHEMA_SNAPSHOT_VERSION = 32
LOC_META_VERSION = 1


@lru_cache(maxsize=1)
def cached_loc_meta(_version: int = LOC_META_VERSION) -> Dict[str, str]:
    """Metadatos de ``c_rnc_loc`` (ligero; sin tablas de ruteo)."""
    from ruteo.routing_engine.schema import loc_meta

    with connect() as conn:
        return loc_meta(conn)


@lru_cache(maxsize=1)
def cached_schema_snapshot(_version: int = SCHEMA_SNAPSHOT_VERSION) -> Dict[str, Any]:
    """
    Snapshot completo: localidades, red y vértices.

    ``column_resolver`` ya cachea ``information_schema``; esto evita
    recomponer el dict en cada petición.
    """
    from ruteo.routing_engine.schema import routing_meta, vertices_meta

    with connect() as conn:
        return {
            "loc": cached_loc_meta(),
            "route": routing_meta(conn),
            "vert": vertices_meta(conn),
        }


def invalidate_schema_cache() -> None:
    """Invalida cachés en memoria (p. ej. tras ALTER TABLE en runtime)."""
    cached_loc_meta.cache_clear()
    cached_schema_snapshot.cache_clear()
