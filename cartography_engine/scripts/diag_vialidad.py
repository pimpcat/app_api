"""Diagnóstico rápido: ejes viales y SIL para una localidad.

Uso (dentro del contenedor api_backend):
  python -m cartography_engine.scripts.diag_vialidad
  python -m cartography_engine.scripts.diag_vialidad 001 0143
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    mun = (sys.argv[1] if len(sys.argv) > 1 else "001").strip().zfill(3)
    loc = (sys.argv[2] if len(sys.argv) > 2 else "0143").strip().zfill(4)

    from cartography_engine.datasource import (
        fetch_layer,
        fetch_vialidad_labels,
        _db_cm,
        _source_srid_for_table,
        _srid_from_settings,
    )
    from cartography_engine.layers import parse_layer_def
    from column_resolver import resolve_column
    import cartography_engine.datasource as ds

    print(f"=== diag_vialidad mun={mun} loc={loc} ===")

    with _db_cm(True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema='marco'
                   AND table_name IN ('e','ea','sil','l','m')
                 ORDER BY 1
                """
            )
            tables = [r["table_name"] for r in (cur.fetchall() or [])]
        print("tablas marco:", tables)

        for t in ("e", "ea"):
            if t not in tables:
                print(f"marco.{t}: NO EXISTE")
                continue
            cols = []
            for cand in (
                "nomvial",
                "cvevial",
                "cvegeo",
                "cve_mun",
                "cve_loc",
                "tipovial",
                "the_geom",
            ):
                found = resolve_column(conn, "marco", t, (cand,))
                if found:
                    cols.append(found)
            print(f"marco.{t} cols relevantes:", cols)
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        f"""
                        SELECT COUNT(*) AS n
                          FROM marco."{t}"
                         WHERE TRIM(cve_mun::text)=%(m)s
                           AND TRIM(cve_loc::text)=%(l)s
                        """,
                        {"m": mun, "l": loc},
                    )
                    print(f"marco.{t} filas filtradas:", (cur.fetchone() or {}).get("n"))
                except Exception as exc:
                    print(f"marco.{t} COUNT error:", exc)

        if "sil" in tables:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT geografico, COUNT(*) AS n
                      FROM marco.sil
                     WHERE TRIM(cve_mun::text)=%(m)s
                       AND TRIM(cve_loc::text)=%(l)s
                     GROUP BY 1
                     ORDER BY 1
                    """,
                    {"m": mun, "l": loc},
                )
                print("SIL por geografico:")
                for r in cur.fetchall() or []:
                    print(f"  {r.get('geografico')}: {r.get('n')}")

        print("--- coords RAW (sin transform) ---")
        for t in ("m", "e", "ea", "sil", "l"):
            if t not in tables:
                continue
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        f"""
                        SELECT ST_SRID(the_geom) AS srid,
                               ST_X(ST_Centroid(the_geom)) AS x,
                               ST_Y(ST_Centroid(the_geom)) AS y,
                               GeometryType(the_geom) AS gtype
                          FROM marco."{t}"
                         WHERE the_geom IS NOT NULL
                           AND TRIM(cve_mun::text)=%(m)s
                           AND TRIM(cve_loc::text)=%(l)s
                         LIMIT 1
                        """,
                        {"m": mun, "l": loc},
                    )
                    r = cur.fetchone() or {}
                    print(
                        f"  marco.{t}: srid={r.get('srid')} "
                        f"xy=({r.get('x')}, {r.get('y')}) type={r.get('gtype')}"
                    )
                except Exception as exc:
                    print(f"  marco.{t}: error {exc}")

        # SRID efectivo que usará el motor para esta localidad
        ds._TABLE_SRC_SRID.clear()
        ds._MARCO_SRC_SRID = None
        _, map_srid = _srid_from_settings()
        print("--- SRID efectivo (motor) ---")
        for t in ("m", "e", "ea", "sil"):
            if t not in tables:
                continue
            try:
                src = _source_srid_for_table(
                    conn, "marco", t, cve_mun=mun, cve_loc=loc
                )
                print(f"  marco.{t} → src={src} map={map_srid}")
            except Exception as exc:
                print(f"  marco.{t}: error {exc}")

    t0 = time.perf_counter()
    ds._MARCO_SRC_SRID = None
    ds._TABLE_SRC_SRID.clear()
    labs = fetch_vialidad_labels(cve_mun=mun, cve_loc=loc, limit=50)
    print(f"fetch_vialidad_labels: {len(labs)} en {time.perf_counter()-t0:.2f}s")
    for lab in labs[:8]:
        g = lab.get("geometry")
        print(" ", lab.get("text"), g)

    if labs:
        xs = [float(lab["geometry"].x) for lab in labs if lab.get("geometry") is not None]
        ys = [float(lab["geometry"].y) for lab in labs if lab.get("geometry") is not None]
        print(
            f"bbox etiquetas (tras transform a map): "
            f"x=[{min(xs):.1f},{max(xs):.1f}] y=[{min(ys):.1f},{max(ys):.1f}]"
        )
        with _db_cm(True) as conn:
            src_m = (
                _source_srid_for_table(conn, "marco", "m", cve_mun=mun, cve_loc=loc)
                if "m" in tables
                else 32614
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ST_XMin(ST_Extent(ST_Transform(
                             ST_SetSRID(the_geom, %(src)s), %(map)s))) AS xmin,
                           ST_XMax(ST_Extent(ST_Transform(
                             ST_SetSRID(the_geom, %(src)s), %(map)s))) AS xmax
                      FROM marco.m
                     WHERE TRIM(cve_mun::text)=%(m)s AND TRIM(cve_loc::text)=%(l)s
                    """,
                    {"src": src_m, "map": map_srid, "m": mun, "l": loc},
                )
                r = cur.fetchone() or {}
                print(f"bbox manzanas map: x=[{r.get('xmin')},{r.get('xmax')}]")
                if xs and r.get("xmin") is not None:
                    overlap = not (
                        max(xs) < float(r["xmin"]) or min(xs) > float(r["xmax"])
                    )
                    print(f"¿etiquetas solapan manzanas en X? {overlap}")
                    if not overlap:
                        print(
                            "ALERTA: CRS/coords de vialidad NO coinciden con manzanas "
                            "→ no se verán en el PDF"
                        )
                        print(
                            "HINT: RAW tipicos → m/ea/sil/l en 3857 (X~-1.1e7); "
                            "e en México LCC 6372 (X~2.7e6). "
                            "No uses 32614 en e/ea solo con UpdateGeometrySRID."
                        )

    for gid, geo in (
        ("Carretera", "Carretera"),
        ("Canal", "Canal"),
        ("Corriente de Agua", "Corriente de Agua"),
    ):
        layer = parse_layer_def(
            {
                "id": f"sil_{gid[:4]}",
                "table": "marco.sil",
                "geometry": "line",
                "optional": True,
                "filter": {
                    "cve_mun": "{cve_mun}",
                    "cve_loc": "{cve_loc}",
                    "geografico": [geo],
                },
                "limit": 5000,
                "symbol": {"type": "line", "stroke": "#000", "width": 1},
            }
        )
        t0 = time.perf_counter()
        data = fetch_layer(layer, cve_mun=mun, cve_loc=loc)
        n = int(getattr(data, "feature_count", 0) or 0)
        empty = data.geometry is None or getattr(data.geometry, "is_empty", True)
        print(
            f"SIL {geo}: features={n} empty={empty} in {time.perf_counter()-t0:.2f}s"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
