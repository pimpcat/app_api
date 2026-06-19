"""
Búsqueda y obtención de localidades (``c_rnc_loc``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from database import get_db
from ruteo.routing_engine.cache import cached_loc_meta
from ruteo.routing_engine.errors import RuteoError
from utils import is_mun_cve3, norm_cve_mun, quote_ident


def format_label(cvegeo: str, nombre: str) -> str:
    return f"{cvegeo} - {nombre}"


def buscar_localidades_rnc(
    q: str = "",
    cve_mun: Optional[str] = None,
    limit: int = 60,
) -> List[Dict[str, str]]:
    """Catálogo filtrable para combos origen/destino."""
    q = (q or "").strip()
    limit = max(1, min(int(limit or 60), 200))
    cve = norm_cve_mun(cve_mun) if cve_mun and is_mun_cve3(cve_mun) else None

    meta = cached_loc_meta()
    q_cve = quote_ident(meta["cvegeo"])
    q_nom = quote_ident(meta["nombre"])
    q_geom = quote_ident(meta["geom"])
    tbl = meta["table"]
    where_parts = [f"{q_geom} IS NOT NULL"]
    params: Dict[str, Any] = {"lim": limit}

    if q:
        where_parts.append(
            f"(TRIM({q_nom}::text) ILIKE %(pat)s OR TRIM({q_cve}::text) ILIKE %(pat)s)"
        )
        params["pat"] = f"%{q}%"

    if cve and meta["cve_mun"]:
        where_parts.append(f"TRIM({quote_ident(meta['cve_mun'])}::text) = %(cve)s")
        params["cve"] = cve

    sql = f"""
        SELECT TRIM({q_cve}::text) AS cvegeo,
               TRIM({q_nom}::text) AS nombre
          FROM {tbl}
         WHERE {' AND '.join(where_parts)}
         ORDER BY {q_nom}
         LIMIT %(lim)s
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    out: List[Dict[str, str]] = []
    for row in rows:
        cvegeo = (row.get("cvegeo") or "").strip()
        nombre = (row.get("nombre") or "").strip()
        if not cvegeo:
            continue
        out.append(
            {
                "cvegeo": cvegeo,
                "nombre": nombre,
                "label": format_label(cvegeo, nombre),
            }
        )
    return out


def fetch_localidades_par(
    conn,
    meta: Dict[str, str],
    cvegeo_origen: str,
    cvegeo_destino: str,
) -> Dict[str, Dict[str, Any]]:
    """Un solo viaje a BD para origen y destino."""
    q_cve = quote_ident(meta["cvegeo"])
    q_nom = quote_ident(meta["nombre"])
    q_geom = quote_ident(meta["geom"])
    node_col = meta.get("node")
    node_sel = f", {quote_ident(node_col)} AS node_id" if node_col else ""
    tbl = meta["table"]

    sql = f"""
        SELECT TRIM({q_cve}::text) AS cvegeo,
               TRIM({q_nom}::text) AS nombre,
               ST_AsGeoJSON(ST_Transform({q_geom}, 4326), 6) AS geom_json
               {node_sel}
          FROM {tbl}
         WHERE TRIM({q_cve}::text) IN (%(origen)s, %(destino)s)
           AND {q_geom} IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"origen": cvegeo_origen, "destino": cvegeo_destino})
        rows = cur.fetchall()

    by_cve: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cve = (row.get("cvegeo") or "").strip()
        if cve:
            by_cve[cve] = row

    if cvegeo_origen not in by_cve:
        raise RuteoError("LOC_NOT_FOUND", f"No se encontró la localidad {cvegeo_origen}.")
    if cvegeo_destino not in by_cve:
        raise RuteoError("LOC_NOT_FOUND", f"No se encontró la localidad {cvegeo_destino}.")
    return by_cve
