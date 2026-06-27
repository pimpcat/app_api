"""
Buscador geográfico offline — consulta multitabla PostGIS (Atlas Guerrero).

Las fuentes indexadas se declaran en ``config/visor/catalog.json`` (bloque ``search``
por capa y opcional ``search_extras``). Ver ``visor_search_loader.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from database import get_db
from tables import qualified
from utils import is_mun_cve3, norm_cve_mun, quote_ident
from visor_search_loader import (
    _entry_in_scope,
    build_search_sql_for_entry,
    geom_lookup_for_table,
    search_index_from_catalog,
    search_limit_per_source,
)

logger = logging.getLogger(__name__)


def _row_to_payload(row: Dict[str, Any]) -> Dict[str, Any] | None:
    lng = row.get("lng")
    lat = row.get("lat")
    if lng is None or lat is None:
        return None
    try:
        lng_f = float(lng)
        lat_f = float(lat)
    except (TypeError, ValueError):
        return None
    if not (-180.0 <= lng_f <= 180.0 and -90.0 <= lat_f <= 90.0):
        return None

    nombre = (row.get("nombre_busqueda") or "").strip()
    if not nombre:
        return None

    tabla = (row.get("tabla_origen") or "").strip().lower()
    geom_tipo = (row.get("geom_tipo") or "point").strip().lower()
    if geom_tipo not in ("point", "centroid", "polygon"):
        geom_tipo = "point"
    if geom_tipo == "centroid":
        geom_tipo = "polygon"

    return {
        "nombre_busqueda": nombre,
        "tipo": row.get("tipo") or "",
        "tabla_origen": tabla,
        "id_origen": row.get("id_origen") or "",
        "geom_tipo": geom_tipo,
        "lng": lng_f,
        "lat": lat_f,
    }


def fetch_lugar_geometria(tabla: str, cvegeo: str) -> Dict[str, Any]:
    """Devuelve un GeoJSON Feature (WGS84) para resaltar un resultado del buscador."""
    tabla_lc = (tabla or "").strip().lower()
    lookup = geom_lookup_for_table(tabla_lc)
    if not lookup:
        raise ValueError("TABLA_NO_SOPORTADA")

    if not lookup.get("highlight", True):
        raise ValueError("TABLA_NO_SOPORTADA")

    clave = (cvegeo or "").strip()
    if not clave:
        raise ValueError("CVEGEO_INVALIDO")

    q_tabla = qualified(lookup["table"])
    id_col = quote_ident(lookup["id_column"])
    q_geom = quote_ident("the_geom")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT ST_AsGeoJSON(
                           ST_Force2D(
                               ST_Transform(ST_MakeValid({q_geom}), 4326)
                           )
                       ) AS geom
                  FROM {q_tabla}
                 WHERE TRIM({id_col}::text) = TRIM(%(cvegeo)s)
                   AND {q_geom} IS NOT NULL
                 LIMIT 1
                """,
                {"cvegeo": clave},
            )
            row = cur.fetchone()

    if not row or not row.get("geom"):
        raise ValueError("GEOMETRIA_NO_ENCONTRADA")

    geometry = json.loads(row["geom"])
    return {
        "type": "Feature",
        "properties": {
            "cvegeo": clave,
            "tabla_origen": tabla_lc,
        },
        "geometry": geometry,
    }


def _fetch_rows_for_entry(
    entry: Dict[str, Any],
    params: Dict[str, Any],
    scoped: bool,
    limit_per_source: int,
) -> List[Dict[str, Any]]:
    sql = build_search_sql_for_entry(entry, scoped=scoped, limit_per_source=limit_per_source)
    rows: List[Dict[str, Any]] = []
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for raw in cur.fetchall():
                item = _row_to_payload(raw)
                if item:
                    rows.append(item)
    return rows


def buscar_lugares(
    q: str,
    cve_mun: Optional[str] = None,
    limit_per_table: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Busca lugares cuyo nombre coincida con ``q`` según el catálogo del visor.

    Cada fuente del catálogo se consulta por separado para que un error en una
    capa (p. ej. columna inexistente) no anule el resto.
    """
    term = (q or "").strip()
    if len(term) < 2:
        return []

    index = search_index_from_catalog()
    if not index:
        logger.warning("buscar_lugares: índice de búsqueda vacío (revisar catalog.json)")
        return []

    cve = norm_cve_mun(cve_mun or "")
    scoped = bool(cve and is_mun_cve3(cve))

    pattern = f"%{term}%"
    lim = limit_per_table if limit_per_table is not None else search_limit_per_source()
    params: Dict[str, Any] = {"query": pattern}
    if scoped:
        params["cve"] = cve

    rows: List[Dict[str, Any]] = []
    for entry in index:
        if not _entry_in_scope(entry, scoped):
            continue
        try:
            rows.extend(_fetch_rows_for_entry(entry, params, scoped, lim))
        except Exception as exc:
            logger.warning(
                "buscar_lugares: fuente %s (%s) omitida: %s",
                entry.get("layer_id"),
                entry.get("table"),
                exc,
            )
    return rows
