"""
Orquestador principal del motor de ruteo.

Flujo:
  1. Metadatos (caché) → localidades → vértices
  2. Sincronización ligera c_rnc → c_rnc_routing
  3. Búsqueda (Dijkstra) vía runner.run_route
  4. GeoJSON + resumen
"""

from __future__ import annotations

import json
from typing import Any, Dict

from database import get_db
from ruteo.routing_engine.cache import cached_schema_snapshot
from ruteo.routing_engine.errors import RuteoError
from ruteo.routing_engine.geojson import (
    build_feature_collection,
    normalize_route_geometry,
)
from ruteo.routing_engine.localities import fetch_localidades_par
from ruteo.routing_engine.schema import ensure_routing_tables_synced
from ruteo.routing_engine.strategies.legacy_sin_peaje import (
    make_route_context,
    run_legacy_route,
    snap_vertices_to_corridor,
)
from ruteo.routing_engine.types import RoutingOptions
from ruteo.routing_engine.vertices import resolve_vertex_ids


def calcular_ruta_rnc(
    cvegeo_origen: str,
    cvegeo_destino: str,
    usar_peajes: bool = True,
    *,
    options: RoutingOptions | None = None,
) -> Dict[str, Any]:
    """
    Calcula ruta óptima entre dos localidades de ``c_rnc_loc``.

    Compatible con el endpoint ``/api/ruteo`` (parámetro ``usar_peajes``).
    ``options`` permite restricciones avanzadas en futuras versiones de la API.
    """
    origen = (cvegeo_origen or "").strip()
    destino = (cvegeo_destino or "").strip()
    if not origen or not destino:
        raise RuteoError("MISSING_PARAMS", "Se requieren cvegeo_origen y cvegeo_destino.")
    if origen == destino:
        raise RuteoError("SAME_LOCALITY", "Origen y destino deben ser localidades distintas.")

    if options is None:
        options = RoutingOptions.from_usar_peajes(usar_peajes)

    schema = cached_schema_snapshot()
    loc_meta = schema["loc"]
    route_meta = schema["route"]
    vert_meta = schema["vert"]

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout TO 90000")

        ensure_routing_tables_synced(conn)

        loc_rows = fetch_localidades_par(conn, loc_meta, origen, destino)
        route_ctx = None
        if options.evitar_peajes:
            route_ctx = make_route_context(
                str(loc_rows[origen].get("nombre") or ""),
                str(loc_rows[destino].get("nombre") or ""),
            )
            options.route_context = route_ctx

        def _snap(conn, loc_meta, cve_o, cve_d, ctx, sv, ev):
            return snap_vertices_to_corridor(
                conn, loc_meta, cve_o, cve_d, ctx, sv, ev
            )

        start_vid, end_vid = resolve_vertex_ids(
            conn,
            loc_meta,
            vert_meta,
            loc_rows,
            origen,
            destino,
            usar_peajes=not options.evitar_peajes,
            route_ctx=route_ctx,
            snap_to_corridor_fn=_snap if route_ctx else None,
        )

        edge_count, length_m, route_geom_json, resumen, _path = run_legacy_route(
            conn,
            route_meta,
            start_vid,
            end_vid,
            usar_peajes,
            route_ctx,
            loc_meta,
            origen,
            destino,
        )

    if edge_count <= 0 or not route_geom_json:
        msg = (
            "No existe ruta sin peajes entre las localidades seleccionadas."
            if options.evitar_peajes
            else "No existe ruta conectada entre las localidades seleccionadas."
        )
        raise RuteoError("NO_ROUTE", msg)

    route_geom = normalize_route_geometry(json.loads(route_geom_json))
    loc_o = loc_rows[origen]
    loc_d = loc_rows[destino]
    length_km = round(length_m / 1000.0, 2)

    return {
        "ok": True,
        "cvegeo_origen": origen,
        "cvegeo_destino": destino,
        "nombre_origen": loc_o.get("nombre"),
        "nombre_destino": loc_d.get("nombre"),
        "start_vertex": start_vid,
        "end_vertex": end_vid,
        "edge_count": edge_count,
        "length_m": round(length_m, 2),
        "length_km": length_km,
        "usar_peajes": usar_peajes,
        "resumen": resumen,
        "geojson": build_feature_collection(
            route_geom, loc_o, loc_d, edge_count, length_m, length_km
        ),
    }
