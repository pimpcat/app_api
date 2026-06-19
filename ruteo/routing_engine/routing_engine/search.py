"""
Ejecución del algoritmo de búsqueda (pgRouting Dijkstra).
"""

from __future__ import annotations

from typing import Any, Dict, List


def fetch_dijkstra_path(
    conn,
    edges_sql: str,
    start_vid: int,
    end_vid: int,
) -> List[Dict[str, Any]]:
    """
    Ejecuta ``pgr_dijkstra`` sin JOIN geométrico ni agregados PostGIS.

    Retorna filas con ``edge``, ``path_seq``, ``agg_cost``.
    """
    from psycopg import sql

    query = sql.SQL(
        """
        SELECT edge::bigint AS edge,
               path_seq::int AS path_seq,
               agg_cost::double precision AS agg_cost
          FROM pgr_dijkstra(
            {},
            {}::bigint,
            {}::bigint,
            directed := false
          )
         WHERE edge > 0
         ORDER BY path_seq
        """
    ).format(
        sql.Literal(edges_sql),
        sql.Literal(start_vid),
        sql.Literal(end_vid),
    )
    with conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchall()
