"""
Buscador geográfico offline — consulta multitabla PostGIS (Atlas Guerrero).

Capas indexadas:
  - atlas.c_l          → Municipios (solo búsqueda estatal sin filtro municipal)
  - atlas.c_loc_punto  → Localidades (punto)
  - atlas.c_col_ase    → Colonias / asentamientos

Con ``cve_mun`` válido la búsqueda se limita a localidades y colonias del municipio.
Coordenadas devueltas en WGS84 (EPSG:4326).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from database import get_db
from tables import T_COL_ASE, T_L, T_LOC_PUNTO, qualified
from utils import is_mun_cve3, mun_where_sql, norm_cve_mun

_WGS84_POINT = """
        ST_X(ST_Transform(the_geom, 4326)) AS lng,
        ST_Y(ST_Transform(the_geom, 4326)) AS lat"""

_WGS84_CENTROID = """
        ST_X(ST_Transform(ST_Centroid(the_geom), 4326)) AS lng,
        ST_Y(ST_Transform(ST_Centroid(the_geom), 4326)) AS lat"""

# --- SQL multitabla (parametrizado; :query ← patrón ILIKE con comodines) ---

_BUSCAR_SQL_ESTATAL = f"""
(
    SELECT
        TRIM(BOTH FROM nomgeo::text) AS nombre_busqueda,
        'Municipio' AS tipo,
        'c_l' AS tabla_origen,
        TRIM(BOTH FROM cvegeo::text) AS id_origen,
        {_WGS84_CENTROID}
    FROM {qualified(T_L)}
    WHERE nomgeo ILIKE %(query)s
    LIMIT %(lim)s
)
UNION ALL
(
    SELECT
        TRIM(BOTH FROM nom_loc::text) AS nombre_busqueda,
        'Localidad' AS tipo,
        'c_loc_punto' AS tabla_origen,
        TRIM(BOTH FROM cvegeo::text) AS id_origen,
        {_WGS84_POINT}
    FROM {qualified(T_LOC_PUNTO)}
    WHERE nom_loc ILIKE %(query)s
    LIMIT %(lim)s
)
UNION ALL
(
    SELECT
        TRIM(BOTH FROM nom_asen::text) AS nombre_busqueda,
        'Colonia/Asentamiento' AS tipo,
        'c_col_ase' AS tabla_origen,
        TRIM(BOTH FROM cvegeo::text) AS id_origen,
        {_WGS84_CENTROID}
    FROM {qualified(T_COL_ASE)}
    WHERE nom_asen ILIKE %(query)s
    LIMIT %(lim)s
)
"""


def _buscar_sql_municipio() -> str:
    """Localidades y colonias dentro del municipio activo (``%(cve)s``)."""
    mun = mun_where_sql("", with_cvegeo=True)
    return f"""
(
    SELECT
        TRIM(BOTH FROM nom_loc::text) AS nombre_busqueda,
        'Localidad' AS tipo,
        'c_loc_punto' AS tabla_origen,
        TRIM(BOTH FROM cvegeo::text) AS id_origen,
        {_WGS84_POINT}
    FROM {qualified(T_LOC_PUNTO)}
    WHERE nom_loc ILIKE %(query)s AND {mun}
    LIMIT %(lim)s
)
UNION ALL
(
    SELECT
        TRIM(BOTH FROM nom_asen::text) AS nombre_busqueda,
        'Colonia/Asentamiento' AS tipo,
        'c_col_ase' AS tabla_origen,
        TRIM(BOTH FROM cvegeo::text) AS id_origen,
        {_WGS84_CENTROID}
    FROM {qualified(T_COL_ASE)}
    WHERE nom_asen ILIKE %(query)s AND {mun}
    LIMIT %(lim)s
)
"""


def _row_to_payload(row: Dict[str, Any]) -> Dict[str, Any] | None:
    """Normaliza una fila del cursor a JSON serializable (descarta geometrías nulas)."""
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
    return {
        "nombre_busqueda": nombre,
        "tipo": row.get("tipo") or "",
        "tabla_origen": row.get("tabla_origen") or "",
        "id_origen": row.get("id_origen") or "",
        "lng": lng_f,
        "lat": lat_f,
    }


def buscar_lugares(
    q: str,
    cve_mun: Optional[str] = None,
    limit_per_table: int = 5,
) -> List[Dict[str, Any]]:
    """
    Busca lugares cuyo nombre coincida con ``q``.

    :param q: Texto libre del usuario (se envuelve con ``%`` para ILIKE).
    :param cve_mun: Clave municipal 3 dígitos; limita a localidades/colonias del municipio.
    :param limit_per_table: Tope por capa en cada rama del UNION ALL.
    :returns: Lista de dicts listos para serializar en ``/api/buscar``.
    """
    term = (q or "").strip()
    if len(term) < 2:
        return []

    cve = norm_cve_mun(cve_mun or "")
    scoped = bool(cve and is_mun_cve3(cve))

    pattern = f"%{term}%"
    lim = max(1, min(int(limit_per_table), 20))
    params: Dict[str, Any] = {"query": pattern, "lim": lim}
    sql = _BUSCAR_SQL_ESTATAL

    if scoped:
        sql = _buscar_sql_municipio()
        params["cve"] = cve

    rows: List[Dict[str, Any]] = []
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for raw in cur.fetchall():
                item = _row_to_payload(raw)
                if item:
                    rows.append(item)
    return rows
