"""Diagnóstico AGEB rurales colindantes (aux.colindantes).

Uso:
  python -m cartography_engine.scripts.diag_colindantes 029 0001
"""

from __future__ import annotations

import sys

from cartography_engine.datasource import _db_cm, _norm_cve3, _norm_cve4, fetch_layer
from cartography_engine.layers import parse_layers_from_template
from cartography_engine.templates_loader import load_template


def main() -> int:
    mun = _norm_cve3(sys.argv[1] if len(sys.argv) > 1 else "029")
    loc = _norm_cve4(sys.argv[2] if len(sys.argv) > 2 else "0001")
    print(f"=== diag colindantes mun={mun} loc={loc} ===")

    with _db_cm(True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM information_schema.tables
                   WHERE table_schema='aux' AND table_name='colindantes'
                ) AS ok
                """
            )
            row = cur.fetchone() or {}
            print("aux.colindantes exists:", row.get("ok") if hasattr(row, "get") else row)

            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_schema='aux' AND table_name='colindantes'
                 ORDER BY ordinal_position
                """
            )
            cols = [
                r["column_name"] if isinstance(r, dict) else r[0]
                for r in (cur.fetchall() or [])
            ]
            print("columns:", cols)
            if not cols:
                return 1

            cur.execute("SELECT COUNT(*) AS n FROM aux.colindantes")
            print("total rows:", dict(cur.fetchone() or {}))

            # conteo por mun
            if "cve_mun" in cols:
                cur.execute(
                    """
                    SELECT COUNT(*) AS n
                      FROM aux.colindantes
                     WHERE TRIM(cve_mun::text)=%(m)s
                    """,
                    {"m": mun},
                )
                print(f"rows mun={mun}:", dict(cur.fetchone() or {}))

            cur.execute(
                """
                SELECT GeometryType(the_geom) AS gtype, COUNT(*) AS n
                  FROM aux.colindantes
                 WHERE the_geom IS NOT NULL
                 GROUP BY 1
                """
            )
            print("geom types:", [dict(r) for r in (cur.fetchall() or [])])

            # cerca de localidad
            cur.execute(
                """
                SELECT COUNT(*) AS n
                  FROM aux.colindantes g
                  JOIN marco.l locpoly
                    ON TRIM(locpoly.cve_mun::text)=%(m)s
                   AND TRIM(locpoly.cve_loc::text)=%(l)s
                 WHERE g.the_geom IS NOT NULL
                   AND ST_DWithin(
                     CASE WHEN ST_SRID(g.the_geom)=0 THEN ST_SetSRID(g.the_geom,3857)
                          ELSE g.the_geom END,
                     CASE WHEN ST_SRID(locpoly.the_geom)=0 THEN ST_SetSRID(locpoly.the_geom,3857)
                          ELSE locpoly.the_geom END,
                     3500.0
                   )
                """,
                {"m": mun, "l": loc},
            )
            print("within 3500m of L:", dict(cur.fetchone() or {}))

    from cartography_engine.datasource import fetch_colindantes_near_localidad

    tpl = load_template("plano_localidad_urbana")
    layers = {L.id: L for L in parse_layers_from_template(tpl)}
    col = layers["colindantes"]
    print("layer symbol dash:", getattr(col.symbol, "dash", None), "width:", getattr(col.symbol, "stroke_width", None))
    data = fetch_layer(col, cve_mun=mun, cve_loc=loc)
    print(
        "fetch_layer colindantes:",
        "count=",
        data.feature_count,
        "geom=",
        None if data.geometry is None else data.geometry.geom_type,
        "empty=",
        getattr(data.geometry, "is_empty", True),
    )
    if data.geometry is None or getattr(data.geometry, "is_empty", True):
        data2 = fetch_colindantes_near_localidad(col, cve_mun=mun, cve_loc=loc)
        print(
            "fallback near:",
            "count=",
            data2.feature_count,
            "geom=",
            None if data2.geometry is None else data2.geometry.geom_type,
            "table=",
            data2.definition.table,
        )
    ageb = layers["ageb"]
    print(
        "ageb symbol dash:",
        getattr(ageb.symbol, "dash", None),
        "stroke:",
        getattr(ageb.symbol, "stroke_color", None),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
