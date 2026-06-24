"""

Buscador geográfico offline — consulta multitabla PostGIS (Atlas Guerrero).



Capas indexadas:

  - atlas.c_mun        → Municipios (polígono; búsqueda estatal)

  - atlas.c_loc_punto  → Localidades puntuales

  - atlas.c_l          → Localidades con amanzanamiento (polígono)

  - atlas.c_col_ase    → Colonias / asentamientos (polígono)



Con ``cve_mun`` válido la búsqueda incluye localidades (punto y amanzanamiento) y colonias

del municipio. Sin filtro municipal también se incluyen municipios a nivel estatal.

Coordenadas devueltas en WGS84 (EPSG:4326).

"""



from __future__ import annotations



import json

from typing import Any, Dict, List, Optional



from database import get_db

from tables import T_COL_ASE, T_L, T_LOC_PUNTO, T_MUN, qualified

from utils import is_mun_cve3, mun_where_sql, norm_cve_mun, quote_ident



_WGS84_POINT = """

        ST_X(ST_Transform(the_geom, 4326)) AS lng,

        ST_Y(ST_Transform(the_geom, 4326)) AS lat"""



_WGS84_CENTROID = """

        ST_X(ST_Transform(ST_Centroid(the_geom), 4326)) AS lng,

        ST_Y(ST_Transform(ST_Centroid(the_geom), 4326)) AS lat"""



TIPO_MUNICIPIO = "Municipio"

TIPO_LOCALIDAD = "Localidad"

TIPO_LOCALIDAD_AMANZ = "Localidad con amanzanamiento"

TIPO_COLONIA = "Colonia/Asentamiento"



# --- SQL multitabla (parametrizado; :query ← patrón ILIKE con comodines) ---



_BUSCAR_SQL_ESTATAL = f"""

(

    SELECT

        TRIM(BOTH FROM nomgeo::text) AS nombre_busqueda,

        '{TIPO_MUNICIPIO}' AS tipo,

        '{T_MUN}' AS tabla_origen,

        TRIM(BOTH FROM cve_mun::text) AS id_origen,

        {_WGS84_CENTROID}

    FROM {qualified(T_MUN)}

    WHERE nomgeo ILIKE %(query)s

    LIMIT %(lim)s

)

UNION ALL

(

    SELECT

        TRIM(BOTH FROM nom_loc::text) AS nombre_busqueda,

        '{TIPO_LOCALIDAD}' AS tipo,

        '{T_LOC_PUNTO}' AS tabla_origen,

        TRIM(BOTH FROM cvegeo::text) AS id_origen,

        {_WGS84_POINT}

    FROM {qualified(T_LOC_PUNTO)}

    WHERE nom_loc ILIKE %(query)s

    LIMIT %(lim)s

)

UNION ALL

(

    SELECT

        TRIM(BOTH FROM nomgeo::text) AS nombre_busqueda,

        '{TIPO_LOCALIDAD_AMANZ}' AS tipo,

        '{T_L}' AS tabla_origen,

        TRIM(BOTH FROM cvegeo::text) AS id_origen,

        {_WGS84_CENTROID}

    FROM {qualified(T_L)}

    WHERE nomgeo ILIKE %(query)s

    LIMIT %(lim)s

)

UNION ALL

(

    SELECT

        TRIM(BOTH FROM nom_asen::text) AS nombre_busqueda,

        '{TIPO_COLONIA}' AS tipo,

        '{T_COL_ASE}' AS tabla_origen,

        TRIM(BOTH FROM cvegeo::text) AS id_origen,

        {_WGS84_CENTROID}

    FROM {qualified(T_COL_ASE)}

    WHERE nom_asen ILIKE %(query)s

    LIMIT %(lim)s

)

"""





def _buscar_sql_municipio() -> str:

    """Localidades (punto y amanzanamiento) y colonias dentro del municipio activo."""

    mun = mun_where_sql("", with_cvegeo=True)

    return f"""

(

    SELECT

        TRIM(BOTH FROM nom_loc::text) AS nombre_busqueda,

        '{TIPO_LOCALIDAD}' AS tipo,

        '{T_LOC_PUNTO}' AS tabla_origen,

        TRIM(BOTH FROM cvegeo::text) AS id_origen,

        {_WGS84_POINT}

    FROM {qualified(T_LOC_PUNTO)}

    WHERE nom_loc ILIKE %(query)s AND {mun}

    LIMIT %(lim)s

)

UNION ALL

(

    SELECT

        TRIM(BOTH FROM nomgeo::text) AS nombre_busqueda,

        '{TIPO_LOCALIDAD_AMANZ}' AS tipo,

        '{T_L}' AS tabla_origen,

        TRIM(BOTH FROM cvegeo::text) AS id_origen,

        {_WGS84_CENTROID}

    FROM {qualified(T_L)}

    WHERE nomgeo ILIKE %(query)s AND {mun}

    LIMIT %(lim)s

)

UNION ALL

(

    SELECT

        TRIM(BOTH FROM nom_asen::text) AS nombre_busqueda,

        '{TIPO_COLONIA}' AS tipo,

        '{T_COL_ASE}' AS tabla_origen,

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

    tabla = (row.get("tabla_origen") or "").strip().lower()

    return {

        "nombre_busqueda": nombre,

        "tipo": row.get("tipo") or "",

        "tabla_origen": tabla,

        "id_origen": row.get("id_origen") or "",

        "geom_tipo": _geom_tipo_tabla(tabla),

        "lng": lng_f,

        "lat": lat_f,

    }





def _geom_tipo_tabla(tabla: str) -> str:

    if tabla in (T_COL_ASE, T_L, T_MUN):

        return "polygon"

    return "point"





_GEOCODER_GEOM_TABLAS = frozenset({T_COL_ASE, T_L, T_MUN})



_GEOCODER_GEOM_ID_COL = {

    T_COL_ASE: "cvegeo",

    T_L: "cvegeo",

    T_MUN: "cve_mun",

}





def fetch_lugar_geometria(tabla: str, cvegeo: str) -> Dict[str, Any]:

    """

    Devuelve un GeoJSON Feature (WGS84) para colonias, localidades con amanzanamiento

    o municipios del buscador.

    """

    tabla_lc = (tabla or "").strip().lower()

    if tabla_lc not in _GEOCODER_GEOM_TABLAS:

        raise ValueError("TABLA_NO_SOPORTADA")



    clave = (cvegeo or "").strip()

    if not clave:

        raise ValueError("CVEGEO_INVALIDO")



    q_tabla = qualified(tabla_lc)

    id_col = _GEOCODER_GEOM_ID_COL.get(tabla_lc, "cvegeo")

    q_id = quote_ident(id_col)

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

                 WHERE TRIM({q_id}::text) = TRIM(%(cvegeo)s)

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


