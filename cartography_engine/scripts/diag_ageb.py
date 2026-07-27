"""Diagnóstico AGEB urbana para una localidad.

Uso (en el contenedor api_backend):
  python -m cartography_engine.scripts.diag_ageb 029 0001
"""

from __future__ import annotations

import sys

from cartography_engine.datasource import (
    _db_cm,
    _norm_cve3,
    _norm_cve4,
    fetch_layer,
    fetch_urban_ageb_labels,
    format_ageb_clave,
)
from cartography_engine.layers import parse_layers_from_template
from cartography_engine.templates_loader import load_template


def main() -> int:
    mun = _norm_cve3(sys.argv[1] if len(sys.argv) > 1 else "029")
    loc = _norm_cve4(sys.argv[2] if len(sys.argv) > 2 else "0001")
    print(f"=== diag AGEB mun={mun} loc={loc} ===")

    with _db_cm(True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema='marco' AND table_name='a'
                 ORDER BY ordinal_position
                """
            )
            cols = [
                r["column_name"] if isinstance(r, dict) else r[0]
                for r in (cur.fetchall() or [])
            ]
            print("marco.a columns:", cols)

            cur.execute(
                """
                SELECT COUNT(*) AS n,
                       COUNT(DISTINCT TRIM(cve_ageb::text)) AS n_ageb
                  FROM marco.a
                 WHERE TRIM(cve_mun::text)=%(m)s AND TRIM(cve_loc::text)=%(l)s
                """,
                {"m": mun, "l": loc},
            )
            row = cur.fetchone() or {}
            print("rows by mun/loc:", dict(row) if hasattr(row, "keys") else row)

            cur.execute(
                """
                SELECT DISTINCT TRIM(cve_ageb::text) AS cve_ageb,
                       GeometryType(the_geom) AS gtype,
                       ST_SRID(the_geom) AS srid
                  FROM marco.a
                 WHERE TRIM(cve_mun::text)=%(m)s AND TRIM(cve_loc::text)=%(l)s
                 ORDER BY 1
                 LIMIT 40
                """,
                {"m": mun, "l": loc},
            )
            print("sample agebs:")
            for r in cur.fetchall() or []:
                d = dict(r) if hasattr(r, "keys") else {}
                raw = str(d.get("cve_ageb") or "")
                print(" ", d, "→", format_ageb_clave(raw))

    tpl = load_template("plano_localidad_urbana")
    layers = {L.id: L for L in parse_layers_from_template(tpl)}
    ageb = layers["ageb"]
    print("template ageb filters:", ageb.attr_filters, "clip_loc=", ageb.clip_to_localidad)
    print("label_limit=", ageb.label_limit, "style=", ageb.label_style, "size=", ageb.label_size)

    data = fetch_layer(ageb, cve_mun=mun, cve_loc=loc)
    print(
        "fetch_layer ageb:",
        "count=",
        data.feature_count,
        "geom=",
        None if data.geometry is None else data.geometry.geom_type,
        "empty=",
        getattr(data.geometry, "is_empty", True),
    )

    labs = fetch_urban_ageb_labels(cve_mun=mun, cve_loc=loc, limit=2000, size=3.0)
    print("fetch_urban_ageb_labels:", len(labs))
    for lab in labs[:30]:
        print(" ", lab.get("text"), lab.get("style"), lab.get("size"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
