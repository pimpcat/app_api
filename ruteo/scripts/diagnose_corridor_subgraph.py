#!/usr/bin/env python3
"""Diagnóstico del subgrafo del corredor OD (sin peajes / stitch)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from database import connect
from ruteo.routing_engine.cache import SCHEMA_SNAPSHOT_VERSION, cached_schema_snapshot
from ruteo.routing_engine.diagnostics.corridor_subgraph import (
    analyze_corridor_subgraph,
    format_report_summary,
)
from ruteo.routing_engine.diagnostics.csv_export import write_candidate_join_edges_csv
from ruteo.routing_engine.diagnostics.geojson_export import report_to_geojson
from ruteo.routing_engine.localities import fetch_localidades_par
from ruteo.routing_engine.strategies.legacy_sin_peaje import make_route_context

OUT = "/app/ruteo/output"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Diagnóstico topológico del corredor OD.")
    p.add_argument("--origen", default="120290001", help="CVEGEO origen")
    p.add_argument("--destino", default="120010001", help="CVEGEO destino")
    p.add_argument(
        "--geojson",
        default=f"{OUT}/corridor_subgraph_diag.geojson",
        help="Ruta de salida GeoJSON",
    )
    p.add_argument(
        "--json",
        default=f"{OUT}/corridor_subgraph_diag.json",
        help="Reporte JSON (vacío para omitir)",
    )
    p.add_argument(
        "--csv",
        default=f"{OUT}/candidate_join_edges.csv",
        help="CSV candidatas (vacío para omitir)",
    )
    p.add_argument("--no-geojson", action="store_true", help="Solo consola")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    schema = cached_schema_snapshot(SCHEMA_SNAPSHOT_VERSION)
    loc_meta = schema["loc"]

    with connect() as conn:
        loc_rows = fetch_localidades_par(conn, loc_meta, args.origen, args.destino)
        if args.origen not in loc_rows or args.destino not in loc_rows:
            print("ERROR: localidad no encontrada en c_rnc_loc", file=sys.stderr)
            return 1

        nombre_o = str(loc_rows[args.origen].get("nombre") or "")
        nombre_d = str(loc_rows[args.destino].get("nombre") or "")
        route_ctx = make_route_context(nombre_o, nombre_d)

        report = analyze_corridor_subgraph(
            conn,
            cvegeo_origen=args.origen,
            cvegeo_destino=args.destino,
            route_ctx=route_ctx,
            loc_meta=loc_meta,
            origen_nombre=nombre_o,
            destino_nombre=nombre_d,
        )

        print("=== Diagnóstico subgrafo corredor OD ===")
        print(format_report_summary(report))
        print()

        if not args.no_geojson:
            fc = report_to_geojson(conn, report)
            payload = json.dumps(fc, ensure_ascii=False, indent=2)
            if args.geojson == "-":
                print(payload)
            else:
                out = Path(args.geojson)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(payload, encoding="utf-8")
                print(f"GeoJSON: {out.resolve()}")

        if args.json:
            jp = Path(args.json)
            jp.parent.mkdir(parents=True, exist_ok=True)
            jp.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"JSON: {jp.resolve()}")

        if args.csv:
            Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
            csv_path = write_candidate_join_edges_csv(args.csv, report.candidate_join_edges)
            print(f"CSV: {csv_path} ({len(report.candidate_join_edges)} filas)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
