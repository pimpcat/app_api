"""Diagnóstico rápido: grafo sin peajes y ruta Chilpancingo → Acapulco."""
from __future__ import annotations

import sys

from database import connect, get_db
from psycopg import sql as pg_sql
from ruteo import (
    _SCHEMA_SNAPSHOT_VERSION,
    _cached_schema_snapshot,
    _edges_sql,
    _edges_sql_candidates,
    _fetch_dijkstra_path,
    _corridor_anchor_candidates,
    _fetch_corridor_named_edge_ids,
    _fetch_corridor_trunk_edge_ids,
    _resolve_connected_corridor_anchors,
    _fetch_localidades_par,
    _len_geog_m_sql,
    _make_route_context,
    _resolve_vertex_ids,
    _routing_edges_sql_corridor_subgraph,
    _run_route,
    _snap_vertex_to_corridor,
    _sum_route_length_m,
    _try_stitched_corridor_route,
    calcular_ruta_rnc,
)

CVE_O, CVE_D = "120290001", "120010001"


def main() -> int:
    _cached_schema_snapshot.cache_clear()
    meta = _cached_schema_snapshot(_SCHEMA_SNAPSHOT_VERSION)["route"]
    sql_sin = _edges_sql(meta, False)
    sql_con = _edges_sql(meta, True)
    sql_paved = meta.get("edges_sql_sin_peaje_paved")

    print("=== edges_sql_sin_peaje (primeros 200 chars) ===")
    print(sql_sin[:200], "...")
    print()
    print("=== Conteos grafo ===")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)::bigint AS n
                  FROM atlas.c_rnc_routing r
                  JOIN atlas.c_rnc c ON c.gid = r.id
                 WHERE UPPER(TRIM(COALESCE(c.peaje::text, 'No'))) IN ('SI', 'SÍ')
                   AND UPPER(TRIM(COALESCE(r.peaje::text, 'No'))) NOT IN ('SI', 'SÍ')
                """
            )
            print(f"Desfase peaje routing vs c_rnc: {cur.fetchone()['n']} tramos")

            for label, edges in (
                ("Con peajes", sql_con),
                ("Sin peajes (full)", sql_sin),
            ):
                q = pg_sql.SQL("SELECT COUNT(*) AS n FROM ({}) g").format(pg_sql.SQL(edges))
                cur.execute(q)
                print(f"{label:20} {cur.fetchone()['n']} aristas")
            if sql_paved:
                q = pg_sql.SQL("SELECT COUNT(*) AS n FROM ({}) g").format(pg_sql.SQL(sql_paved))
                cur.execute(q)
                print(f"Sin peajes (paved): {cur.fetchone()['n']} aristas (intento preferente)")
            print(
                f"Candidatos Dijkstra: {len(_edges_sql_candidates(meta, False, _make_route_context('Chilpancingo de los Bravo', 'Acapulco de Juárez')))}"
            )

    print()
    print("=== Comparación con / sin peajes ===")
    r_con = calcular_ruta_rnc(CVE_O, CVE_D, usar_peajes=True)
    print(
        f"Con peajes:    {r_con.get('length_km')} km, "
        f"km_peaje={(r_con.get('resumen') or {}).get('km_peaje')}"
    )

    print()
    print("=== Ruta sin peajes ===")
    try:
        r = calcular_ruta_rnc(CVE_O, CVE_D, usar_peajes=False)
        print(f"usar_peajes en respuesta: {r.get('usar_peajes')}")
        print(f"km: {r.get('length_km')}, tramos: {r.get('edge_count')}")
        res = r.get("resumen") or {}
        print(f"km_peaje en ruta: {res.get('km_peaje')}")
        print(f"por_peaje: {res.get('por_peaje')}")
        print(f"por_pavimentado: {res.get('por_pavimentado')}")
        print(f"por_cond_pav: {res.get('por_cond_pav')}")

        schema = _cached_schema_snapshot(_SCHEMA_SNAPSHOT_VERSION)
        with get_db() as conn:
            loc_rows = _fetch_localidades_par(conn, schema["loc"], CVE_O, CVE_D)
            route_ctx = _make_route_context(
                str(loc_rows[CVE_O].get("nombre") or ""),
                str(loc_rows[CVE_D].get("nombre") or ""),
            )
            start_vid, end_vid = _resolve_vertex_ids(
                conn,
                schema["loc"],
                schema["vert"],
                loc_rows,
                CVE_O,
                CVE_D,
                usar_peajes=False,
                route_ctx=route_ctx,
            )
            anchor_o = _snap_vertex_to_corridor(conn, schema["loc"], CVE_O, route_ctx)
            anchor_d = _snap_vertex_to_corridor(conn, schema["loc"], CVE_D, route_ctx)
            print(f"Anclas corredor: origen={anchor_o}, destino={anchor_d}")
            stitched = _try_stitched_corridor_route(
                conn,
                schema["route"],
                schema["loc"],
                start_vid,
                end_vid,
                CVE_O,
                CVE_D,
                route_ctx,
            )
            if stitched:
                sm = _sum_route_length_m(
                    conn, schema["route"], [int(r["edge"]) for r in stitched]
                )
                print(f"Ruta en 3 tramos (stitched): {len(stitched)} tramos, ~{sm/1000:.1f} km")
            else:
                print("Ruta en 3 tramos (stitched): NO conecta")
                routing_tbl = schema["route"].get("routing_table")
                if routing_tbl and anchor_o and anchor_d:
                    named_ids = _fetch_corridor_named_edge_ids(conn, route_ctx)
                    trunk_ids = _fetch_corridor_trunk_edge_ids(conn, route_ctx)
                    print(
                        f"  aristas subgrafo corredor: {len(trunk_ids)} "
                        f"(nombradas: {len(named_ids)})"
                    )
                    co = _corridor_anchor_candidates(conn, schema["loc"], CVE_O, named_ids)
                    cd = _corridor_anchor_candidates(conn, schema["loc"], CVE_D, named_ids)
                    print(f"  candidatos ancla O/D: {len(co)} / {len(cd)}")
                    trunk_sql = _routing_edges_sql_corridor_subgraph(
                        routing_tbl, route_ctx, trunk_ids
                    )
                    if trunk_sql:
                        resolved = _resolve_connected_corridor_anchors(
                            conn,
                            schema["route"],
                            schema["loc"],
                            CVE_O,
                            CVE_D,
                            route_ctx,
                            trunk_ids,
                            trunk_sql,
                        )
                        if resolved:
                            vo, vd, rows = resolved
                            km = _sum_route_length_m(
                                conn, schema["route"], [int(r["edge"]) for r in rows]
                            ) / 1000.0
                            print(
                                f"  tronco conectado {vo}→{vd}: "
                                f"{len(rows)} tramos, ~{km:.1f} km"
                            )
                        else:
                            print("  tronco subgrafo: sin par de anclas conectado")
            _, _, _, _, path_rows = _run_route(
                conn,
                schema["route"],
                start_vid,
                end_vid,
                False,
                route_ctx,
                schema["loc"],
                CVE_O,
                CVE_D,
            )
            edge_ids = [int(r["edge"]) for r in path_rows]
            len_m = _len_geog_m_sql("c.the_geom")
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT TRIM(COALESCE(c.nombre, 'N/D')) AS nombre,
                           TRIM(COALESCE(c.peaje, 'N/D')) AS peaje,
                           ROUND(
                             (SUM(
                               COALESCE(r.longitud_m, {len_m})
                             ) / 1000.0)::numeric,
                             1
                           ) AS km
                      FROM unnest(%(ids)s::bigint[]) AS u(gid)
                      JOIN atlas.c_rnc c ON c.gid = u.gid
                      LEFT JOIN atlas.c_rnc_routing r ON r.id = u.gid
                     GROUP BY 1, 2
                     ORDER BY km DESC
                     LIMIT 12
                    """,
                    {"ids": edge_ids},
                )
                rows = cur.fetchall()
        print()
        print("=== Top vialidades en ruta sin peajes (por km) ===")
        for row in rows:
            print(f"  {row['km']:>6} km  peaje={row['peaje']:<3}  {row['nombre']}")
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
