"""
Herramientas de diagnóstico del motor de ruteo (sin calcular rutas).
"""

from ruteo.routing_engine.diagnostics.corridor_subgraph import (
    analyze_corridor_subgraph,
    fetch_trunk_with_bridge_meta,
    format_report_summary,
)
from ruteo.routing_engine.diagnostics.csv_export import write_candidate_join_edges_csv
from ruteo.routing_engine.diagnostics.geojson_export import report_to_geojson
from ruteo.routing_engine.diagnostics.spatial import (
    SpatialContext,
    detect_spatial_context,
    detect_spatial_context_conn,
)

__all__ = [
    "analyze_corridor_subgraph",
    "fetch_trunk_with_bridge_meta",
    "format_report_summary",
    "report_to_geojson",
    "write_candidate_join_edges_csv",
    "SpatialContext",
    "detect_spatial_context",
    "detect_spatial_context_conn",
]
