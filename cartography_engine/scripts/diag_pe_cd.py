"""Diagnóstico PE/CD en GroSIG_Cartography."""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")

from cartography_engine.datasource import _db_cm  # noqa: E402


def main() -> None:
    with _db_cm(True) as conn:
        with conn.cursor() as cur:
            for t in ("pe", "cd", "l", "m"):
                cur.execute(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema='marco' AND table_name=%s
                     ORDER BY ordinal_position
                    """,
                    (t,),
                )
                cols = [r["column_name"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
                print(f"{t} COLS={cols}")

            for t in ("pe", "cd"):
                cur.execute(f"SELECT COUNT(*) AS n FROM marco.{t}")
                print(f"{t} TOTAL={cur.fetchone()}")
                cur.execute(
                    f"""
                    SELECT COUNT(*) AS n FROM marco.{t}
                     WHERE TRIM(BOTH FROM COALESCE(cve_mun::text,'')) = '001'
                    """
                )
                print(f"{t} MUN001={cur.fetchone()}")
                try:
                    cur.execute(
                        f"""
                        SELECT COUNT(*) AS n FROM marco.{t}
                         WHERE TRIM(BOTH FROM COALESCE(cve_mun::text,'')) = '001'
                           AND TRIM(BOTH FROM COALESCE(cve_loc::text,'')) = '0143'
                        """
                    )
                    print(f"{t} LOC0143={cur.fetchone()}")
                except Exception as e:
                    print(f"{t} LOC_ERR={e}")
                    conn.rollback()
                try:
                    cur.execute(
                        f"""
                        SELECT TRIM(cve_mun::text) AS cve_mun,
                               TRIM(COALESCE(cve_loc::text,'')) AS cve_loc,
                               GeometryType(the_geom) AS gt
                          FROM marco.{t}
                         WHERE TRIM(BOTH FROM COALESCE(cve_mun::text,'')) = '001'
                         LIMIT 10
                        """
                    )
                    print(f"{t} SAMPLE={cur.fetchall()}")
                except Exception as e:
                    print(f"{t} SAMPLE_ERR={e}")
                    conn.rollback()

            # ¿CD/PE ligados por otra clave?
            for t in ("pe", "cd"):
                try:
                    cur.execute(
                        f"""
                        SELECT column_name FROM information_schema.columns
                         WHERE table_schema='marco' AND table_name=%s
                           AND (column_name ILIKE '%%loc%%'
                             OR column_name ILIKE '%%mza%%'
                             OR column_name ILIKE '%%geo%%'
                             OR column_name ILIKE '%%cve%%')
                        """,
                        (t,),
                    )
                    print(f"{t} KEYCOLS={cur.fetchall()}")
                except Exception as e:
                    print(f"{t} KEY_ERR={e}")
                    conn.rollback()


if __name__ == "__main__":
    main()
