"""
Estrategia legacy «sin peajes»: corredor OD, stitch y contexto de ruta.
"""

from __future__ import annotations

import re
from typing import Tuple

from ruteo.routing_engine.runner import run_route
from ruteo.routing_engine.strategies.od_corridor import snap_vertex_to_corridor
from ruteo.routing_engine.types import RouteContext

_TOKEN_STOPWORDS = frozenset(
    {"de", "del", "la", "las", "los", "el", "y", "san", "santa", "santo", "van", "von"}
)


def extract_localidad_tokens(nombre: str) -> Tuple[str, ...]:
    """Palabras clave del nombre de localidad (p. ej. chilpancingo, acapulco)."""
    words = re.findall(r"[a-záéíóúñü]+", (nombre or "").lower())
    out: list[str] = []
    for w in words:
        if len(w) >= 4 and w not in _TOKEN_STOPWORDS:
            out.append(w.replace("'", "''"))
    return tuple(dict.fromkeys(out))[:4]


def make_route_context(origen_nombre: str, destino_nombre: str) -> RouteContext:
    return RouteContext(
        origen_nombre=(origen_nombre or "").strip(),
        destino_nombre=(destino_nombre or "").strip(),
        tokens_o=extract_localidad_tokens(origen_nombre),
        tokens_d=extract_localidad_tokens(destino_nombre),
    )


def run_legacy_route(
    conn,
    route_meta,
    start_vid: int,
    end_vid: int,
    usar_peajes: bool,
    route_ctx: RouteContext | None,
    loc_meta,
    cvegeo_origen: str,
    cvegeo_destino: str,
):
    """Ejecuta ``runner.run_route`` (stitch, scoring, validación peaje)."""
    return run_route(
        conn,
        route_meta,
        start_vid,
        end_vid,
        usar_peajes,
        route_ctx,
        loc_meta,
        cvegeo_origen,
        cvegeo_destino,
    )


def snap_vertices_to_corridor(
    conn,
    loc_meta,
    cvegeo_origen: str,
    cvegeo_destino: str,
    route_ctx: RouteContext,
    start_vid: int,
    end_vid: int,
):
    """Refina vértices al corredor OD (modo sin peajes)."""
    new_o = snap_vertex_to_corridor(conn, loc_meta, cvegeo_origen, route_ctx)
    new_d = snap_vertex_to_corridor(conn, loc_meta, cvegeo_destino, route_ctx)
    return new_o or start_vid, new_d or end_vid
