"""
Análisis espacial dinámico sobre capas PostGIS del Atlas.

Permite:
  1. Descubrir columnas numéricas vía information_schema (sin modelos Pydantic gigantes).
  2. Agregar (SUM/AVG) campos elegidos dentro de un polígono GeoJSON (WGS84).

Seguridad:
  - Solo tablas registradas en CAPAS_ANALISIS.
  - Columnas validadas contra information_schema antes de armar el SELECT.
  - Identificadores escapados con quote_ident().
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from tables import SCHEMA, T_C_INV, T_CLUES, T_DENUE, T_ITER, T_LOC_PUNTO, qualified
from utils import norm_cve_mun, quote_ident

# ---------------------------------------------------------------------------
# Catálogo de capas habilitadas para análisis espacial (extensible).
# ---------------------------------------------------------------------------
CAPAS_ANALISIS: Dict[str, Dict[str, Any]] = {
    "c_inv": {
        "id": "c_inv",
        "tabla": T_C_INV,
        "etiqueta": "Inventario Nacional de Vivienda (INV) 2020",
        "descripcion": "Manzanas INV 2020 (atlas.c_inv)",
        "geom_column": "the_geom",
        "srid_almacenamiento": 900913,
        "grupo": "censales",
        "modo": "agregacion",
    },
    "iter": {
        "id": "iter",
        "tabla": T_ITER,
        "etiqueta": "ITER 2020 — Localidades",
        "descripcion": "Indicadores ITER por localidad (geometría en atlas.c_loc_punto)",
        "geom_column": "the_geom",
        "geom_tabla": T_LOC_PUNTO,
        "join_column": "cvegeo",
        "srid_almacenamiento": 900913,
        "grupo": "censales",
        "modo": "agregacion",
    },
}

# DENUE (atlas.c_denue) — mismos códigos SCIAN que js/denueLayers.js
_DENUE_CAPAS_SPECS: List[Dict[str, Any]] = [
    {"id": "denue_rastros", "etiqueta": "Rastros", "codigo_act": [311611]},
    {"id": "denue_gasolinerias", "etiqueta": "Gasolinerías", "codigo_act": [468411]},
    {"id": "denue_gaseras", "etiqueta": "Gaseras", "codigo_act": [468412]},
    {
        "id": "denue_escuelas",
        "etiqueta": "Escuelas",
        "codigo_act": [
            611112, 611122, 611132, 611142, 611152, 611162, 611172, 611182,
            611212, 611312, 611422, 611432, 611512, 611612, 611622, 611632,
        ],
    },
    {"id": "denue_hospitales", "etiqueta": "Hospitales (DENUE)", "codigo_act": [622112]},
    {"id": "denue_cementerios", "etiqueta": "Cementerios", "codigo_act": [812322]},
    {"id": "denue_iglesias", "etiqueta": "Iglesias/Templos", "codigo_act": [813210]},
    {"id": "denue_museos", "etiqueta": "Museos", "codigo_act": [712112]},
]

for _denue in _DENUE_CAPAS_SPECS:
    CAPAS_ANALISIS[_denue["id"]] = {
        "id": _denue["id"],
        "tabla": T_DENUE,
        "etiqueta": _denue["etiqueta"],
        "descripcion": f"DENUE — {_denue['etiqueta']}",
        "geom_column": "the_geom",
        "modo": "conteo",
        "grupo": "denue",
        "codigo_act": _denue["codigo_act"],
    }

CAPAS_ANALISIS["clues"] = {
    "id": "clues",
    "tabla": T_CLUES,
    "etiqueta": "Establecimientos de salud / Secretaría de Salud",
    "descripcion": "Establecimientos de salud (atlas.c_clues)",
    "geom_column": "the_geom",
    "modo": "conteo",
    "grupo": "salud",
}

# Campos permitidos para análisis espacial sobre INV 2020 (población → vivienda).
INV_CAMPOS_ANALISIS: List[Dict[str, str]] = [
    {"columna": "pobtot", "etiqueta": "Población total", "agregacion": "sum"},
    {"columna": "pobfem", "etiqueta": "Población femenina", "agregacion": "sum"},
    {"columna": "pobmas", "etiqueta": "Población masculina", "agregacion": "sum"},
    {"columna": "pob0_14", "etiqueta": "Población de 0 a 14 años", "agregacion": "sum"},
    {"columna": "p15a29a", "etiqueta": "Población de 15 a 29 años", "agregacion": "sum"},
    {"columna": "p30a59a", "etiqueta": "Población de 30 a 59 años", "agregacion": "sum"},
    {"columna": "p_60ymas", "etiqueta": "Población de 60 años y más", "agregacion": "sum"},
    {"columna": "p_cd_t", "etiqueta": "Población con discapacidad", "agregacion": "sum"},
    {"columna": "vivtot", "etiqueta": "Total de viviendas", "agregacion": "sum"},
    {"columna": "vivpar", "etiqueta": "Total de viviendas particulares", "agregacion": "sum"},
    {"columna": "tvipahab", "etiqueta": "Total de viviendas particulares habitadas", "agregacion": "sum"},
    {"columna": "vivnohab", "etiqueta": "Viviendas particulares no habitadas", "agregacion": "sum"},
    {
        "columna": "v3masocu",
        "etiqueta": "Viviendas particulares habitadas con 3 o más ocupantes por cuarto",
        "agregacion": "sum",
    },
    {
        "columna": "vph_pidt",
        "etiqueta": "Viviendas particulares habitadas con piso de material diferente de tierra",
        "agregacion": "sum",
    },
    {
        "columna": "vph_c_el",
        "etiqueta": "Viviendas particulares habitadas que disponen de energía eléctrica",
        "agregacion": "sum",
    },
    {
        "columna": "vph_exsa",
        "etiqueta": "Viviendas particulares habitadas que disponen de excusado o sanitario",
        "agregacion": "sum",
    },
    {
        "columna": "vph_dren",
        "etiqueta": "Viviendas particulares habitadas que disponen de drenaje",
        "agregacion": "sum",
    },
]

_INV_CAMPOS_MAP: Dict[str, Dict[str, str]] = {c["columna"]: c for c in INV_CAMPOS_ANALISIS}

# Campos ITER 2020 (atlas.iter, geometría vía atlas.c_loc_punto.cvegeo).
ITER_CAMPOS_ANALISIS: List[Dict[str, str]] = [
    {"columna": "pobtot", "etiqueta": "Población total", "agregacion": "sum"},
    {"columna": "pobfem", "etiqueta": "Población femenina", "agregacion": "sum"},
    {"columna": "pobmas", "etiqueta": "Población masculina", "agregacion": "sum"},
    {
        "columna": "p3ym_hli",
        "etiqueta": "Población de 3 años y más que habla alguna lengua indígena",
        "agregacion": "sum",
    },
    {
        "columna": "p3hlinhe",
        "etiqueta": "Población de 3 años y más que habla alguna lengua indígena y no habla español",
        "agregacion": "sum",
    },
    {"columna": "pcon_disc", "etiqueta": "Población con discapacidad", "agregacion": "sum"},
    {
        "columna": "psind_lim",
        "etiqueta": "Población sin discapacidad, limitación, problema o condición mental",
        "agregacion": "sum",
    },
    {
        "columna": "pea",
        "etiqueta": "Población de 12 años y más económicamente activa",
        "agregacion": "sum",
    },
    {
        "columna": "pea_f",
        "etiqueta": "Población femenina de 12 años y más económicamente activa",
        "agregacion": "sum",
    },
    {
        "columna": "pea_m",
        "etiqueta": "Población masculina de 12 años y más económicamente activa",
        "agregacion": "sum",
    },
    {
        "columna": "psinder",
        "etiqueta": "Población sin afiliación a servicios de salud",
        "agregacion": "sum",
    },
    {
        "columna": "pder_ss",
        "etiqueta": "Población afiliada a servicios de salud",
        "agregacion": "sum",
    },
    {"columna": "vivtot", "etiqueta": "Total de viviendas", "agregacion": "sum"},
    {"columna": "tvivhab", "etiqueta": "Total de viviendas habitadas", "agregacion": "sum"},
    {"columna": "tvivpar", "etiqueta": "Total de viviendas particulares", "agregacion": "sum"},
    {"columna": "vivpar_hab", "etiqueta": "Viviendas particulares habitadas", "agregacion": "sum"},
    {"columna": "vivpar_des", "etiqueta": "Viviendas particulares deshabitadas", "agregacion": "sum"},
    {
        "columna": "vph_pisodt",
        "etiqueta": "Viviendas particulares habitadas con piso de material diferente de tierra",
        "agregacion": "sum",
    },
    {
        "columna": "vph_pisoti",
        "etiqueta": "Viviendas particulares habitadas con piso de tierra",
        "agregacion": "sum",
    },
    {
        "columna": "vph_c_elec",
        "etiqueta": "Viviendas particulares habitadas que disponen de energía eléctrica",
        "agregacion": "sum",
    },
    {
        "columna": "vph_aguadv",
        "etiqueta": "Viviendas particulares habitadas que disponen de agua entubada en el ámbito de la vivienda",
        "agregacion": "sum",
    },
    {
        "columna": "vph_drenaj",
        "etiqueta": "Viviendas particulares habitadas que disponen de drenaje",
        "agregacion": "sum",
    },
]

_ITER_CAMPOS_MAP: Dict[str, Dict[str, str]] = {c["columna"]: c for c in ITER_CAMPOS_ANALISIS}

# Campos del INV que no deben ofrecerse para SUM/AVG (identificadores / geometría / categóricos fijos).
_EXCLUIR_CAMPOS = frozenset({
    "gid",
    "ogc_fid",
    "the_geom",
    "geom",
    "wkb_geometry",
    "cvegeo",
    "cve_mza",
    "cve_ent",
    "cve_loc",
    "cve_mun",
    "cve_ageb",
    "ambito",
    "tipomza",
    "nomgeo",
    "nom_ent",
    "nom_mun",
    "nom_loc",
    "nom_ageb",
})

# Tipos Postgres con valor numérico nativo.
_TIPOS_SUMA = frozenset({"smallint", "integer", "bigint"})
_TIPOS_PROMEDIO = frozenset({"numeric", "double precision", "real"})
# INV 2020 suele cargar indicadores como texto; se agregan con ::numeric en SQL.
_TIPOS_TEXTO_NUMERICO = frozenset({"character varying", "text", "character"})

_TABLA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_CAMPO_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def listar_capas_disponibles() -> List[Dict[str, str]]:
    """Lista capas expuestas al frontend para el menú desplegable."""
    orden_grupo = {"censales": 0, "denue": 1, "salud": 2}
    items = [
        {
            "id": meta["id"],
            "tabla": meta["tabla"],
            "etiqueta": meta["etiqueta"],
            "descripcion": meta.get("descripcion", ""),
            "grupo": meta.get("grupo", "otros"),
            "modo": meta.get("modo", "agregacion"),
        }
        for meta in CAPAS_ANALISIS.values()
    ]
    items.sort(key=lambda c: (orden_grupo.get(c["grupo"], 9), c["etiqueta"]))
    return items


def _es_capa_conteo(meta: Mapping[str, Any]) -> bool:
    return meta.get("modo") == "conteo"


def _sql_mun_filter(alias: str, cve: Optional[str]) -> str:
    if not cve:
        return ""
    return f" AND TRIM({alias}.cve_mun::text) = %(cve_mun)s"


def _sql_codigo_act_filter(alias: str, codes: Sequence[int]) -> str:
    """Filtro codigo_act (varchar en c_denue) — solo comparación como texto."""
    safe = [int(c) for c in codes if str(c).isdigit()]
    if not safe:
        return "FALSE"
    col = f"TRIM({alias}.codigo_act::text)"
    tests = " OR ".join(f"{col} = '{c}'" for c in safe)
    return f"({tests})"


def _sql_filtros_capa_conteo(meta: Mapping[str, Any], alias: str, cve: Optional[str]) -> str:
    parts = list(_sql_filtro_interseccion_poligono(alias, meta.get("geom_column", "the_geom")))
    codes = meta.get("codigo_act")
    if codes:
        parts.append(_sql_codigo_act_filter(alias, codes))
    mun = _sql_mun_filter(alias, cve)
    if mun:
        parts.append(mun.lstrip(" AND "))
    return " AND ".join(parts)


def _resolver_meta_tabla(nombre_tabla: str) -> Dict[str, Any]:
    clave = (nombre_tabla or "").strip().lower()
    if clave not in CAPAS_ANALISIS:
        raise ValueError("TABLA_NO_PERMITIDA")
    return CAPAS_ANALISIS[clave]


def _etiqueta_columna(tabla: str, columna: str) -> str:
    lc = columna.lower()
    if tabla == T_C_INV and lc in _INV_CAMPOS_MAP:
        return _INV_CAMPOS_MAP[lc]["etiqueta"]
    if tabla == T_ITER and lc in _ITER_CAMPOS_MAP:
        return _ITER_CAMPOS_MAP[lc]["etiqueta"]
    return lc.replace("_", " ").upper()


def _columnas_inv_analisis() -> List[Dict[str, str]]:
    """Catálogo fijo INV: todos los campos se agregan con cast texto → numeric."""
    return [
        {
            "columna": c["columna"],
            "tipo": "character varying",
            "agregacion": c["agregacion"],
            "etiqueta": c["etiqueta"],
            "cast": "text_numeric",
        }
        for c in INV_CAMPOS_ANALISIS
    ]


def _columnas_iter_analisis() -> List[Dict[str, str]]:
    """Catálogo fijo ITER: indicadores por localidad (join con c_loc_punto)."""
    return [
        {
            "columna": c["columna"],
            "tipo": "character varying",
            "agregacion": c["agregacion"],
            "etiqueta": c["etiqueta"],
            "cast": "text_numeric",
        }
        for c in ITER_CAMPOS_ANALISIS
    ]


def _infer_agregacion(columna: str, tipo: str) -> str:
    """SUM para conteos; AVG para promedios de escolaridad y tipos float/numeric."""
    lc = columna.lower()
    if lc.startswith("graproes") or tipo in _TIPOS_PROMEDIO:
        return "avg"
    return "sum"


def _es_columna_agregable(lc: str, tipo: str, geom_col: str) -> bool:
    if not lc or lc == geom_col or lc in _EXCLUIR_CAMPOS:
        return False
    if not _CAMPO_RE.match(lc):
        return False
    if tipo in _TIPOS_SUMA or tipo in _TIPOS_PROMEDIO:
        return True
    # atlas.c_inv: la mayoría de indicadores INV vienen como varchar desde el ETL.
    if tipo in _TIPOS_TEXTO_NUMERICO:
        return True
    return False


def listar_columnas_numericas(conn, nombre_tabla: str) -> List[Dict[str, str]]:
    """
    Columnas disponibles para el selector del análisis espacial.

    Para INV 2020 devuelve el catálogo fijo acordado; otras capas usan information_schema.
    """
    meta = _resolver_meta_tabla(nombre_tabla)
    if _es_capa_conteo(meta):
        return []
    if meta["id"] == "c_inv":
        return _columnas_inv_analisis()
    if meta["id"] == "iter":
        return _columnas_iter_analisis()

    tabla = meta["tabla"]
    geom_col = meta["geom_column"].lower()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = %s
               AND table_name = %s
             ORDER BY ordinal_position
            """,
            (SCHEMA, tabla),
        )
        rows = cur.fetchall()

    columnas: List[Dict[str, str]] = []
    for row in rows:
        nombre = (row.get("column_name") or "").strip()
        if not nombre:
            continue
        lc = nombre.lower()
        tipo = (row.get("data_type") or "").strip().lower()
        if not _es_columna_agregable(lc, tipo, geom_col):
            continue
        agg = _infer_agregacion(lc, tipo)
        item: Dict[str, str] = {
            "columna": lc,
            "tipo": tipo,
            "agregacion": agg,
            "etiqueta": _etiqueta_columna(tabla, lc),
        }
        if tipo in _TIPOS_TEXTO_NUMERICO:
            item["cast"] = "text_numeric"
        columnas.append(item)
    return columnas


def _extraer_geometria_poligono(geojson: Any) -> Dict[str, Any]:
    """
    Normaliza Feature / FeatureCollection / Geometry a un GeoJSON Geometry Polygon.
    """
    if not geojson or not isinstance(geojson, dict):
        raise ValueError("GEOJSON_INVALIDO")

    tipo = geojson.get("type")
    if tipo == "Feature":
        geom = geojson.get("geometry")
    elif tipo == "FeatureCollection":
        features = geojson.get("features") or []
        geom = None
        for feat in features:
            if not isinstance(feat, dict):
                continue
            g = feat.get("geometry")
            if g and g.get("type") in ("Polygon", "MultiPolygon"):
                geom = g
                break
        if geom is None and features:
            geom = features[0].get("geometry")
    elif tipo in ("Polygon", "MultiPolygon"):
        geom = geojson
    else:
        geom = geojson.get("geometry") if isinstance(geojson.get("geometry"), dict) else None

    if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("GEOMETRIA_NO_POLIGONO")
    return geom


def _parse_geodesic_circle(geojson: Any) -> Optional[Tuple[float, float, float]]:
    """
    Círculos de MapboxDrawGeodesic: centro en coordinates + circleRadius (km).
    """
    if not isinstance(geojson, dict) or geojson.get("type") != "Feature":
        return None
    props = geojson.get("properties") or {}
    radius_km = props.get("circleRadius")
    if not isinstance(radius_km, (int, float)) or radius_km <= 0:
        return None
    geom = geojson.get("geometry") or {}
    if geom.get("type") != "Polygon":
        return None
    ring = (geom.get("coordinates") or [[]])[0]
    if not ring:
        return None
    center = ring[0]
    if not isinstance(center, (list, tuple)) or len(center) < 2:
        return None
    return float(center[0]), float(center[1]), float(radius_km)


def _sql_cte_poligono(geojson: Any) -> Tuple[str, Dict[str, Any]]:
    """CTE poly: GeoJSON normal o ST_Buffer para círculos geodésicos del visor."""
    circle = _parse_geodesic_circle(geojson)
    if circle:
        lng, lat, radius_km = circle
        sql = """
            SELECT ST_MakeValid(
                       ST_Buffer(
                           ST_SetSRID(ST_MakePoint(%(circle_lng)s, %(circle_lat)s), 4326)::geography,
                           %(circle_radius_m)s
                       )::geometry
                   ) AS geom4326
        """
        return sql, {
            "circle_lng": lng,
            "circle_lat": lat,
            "circle_radius_m": radius_km * 1000.0,
        }

    geom = _extraer_geometria_poligono(geojson)
    geom_json = json.dumps(geom, ensure_ascii=False)
    sql = """
            SELECT ST_MakeValid(
                       ST_SetSRID(ST_GeomFromGeoJSON(%(geojson)s), 4326)
                   ) AS geom4326
    """
    return sql, {"geojson": geom_json}


def _validar_campos_solicitados(
    conn,
    nombre_tabla: str,
    campos: Sequence[str],
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Cruza campos pedidos con el catálogo permitido; rechaza desconocidos."""
    disponibles = {c["columna"]: c for c in listar_columnas_numericas(conn, nombre_tabla)}
    validados: List[Dict[str, str]] = []
    for raw in campos:
        clave = (raw or "").strip().lower()
        if not clave or not _CAMPO_RE.match(clave):
            raise ValueError("CAMPO_INVALIDO")
        if clave not in disponibles:
            raise ValueError(f"CAMPO_NO_PERMITIDO:{clave}")
        validados.append(disponibles[clave])
    if not validados:
        raise ValueError("SIN_CAMPOS")
    return validados, [c["columna"] for c in validados]


def _sql_expr_columna(col: Mapping[str, str], alias: str = "t") -> str:
    """Expresión SQL segura para agregar (incluye varchar → numeric en INV/ITER)."""
    qn = f"{alias}.{quote_ident(col['columna'])}"
    if col.get("cast") == "text_numeric":
        cleaned = (
            f"NULLIF(regexp_replace(TRIM({qn}::text), '[^0-9\\.-]', '', 'g'), '')"
        )
        return (
            f"CASE WHEN {cleaned} ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            f"THEN {cleaned}::numeric END"
        )
    return qn


def _sql_agregaciones(
    columnas_meta: Sequence[Dict[str, str]],
    alias: str = "t",
) -> str:
    """Construye la lista SUM(col) / AVG(col) validada."""
    partes: List[str] = []
    for col in columnas_meta:
        nombre = col["columna"]
        expr = _sql_expr_columna(col, alias)
        alias_col = quote_ident(nombre)
        if col["agregacion"] == "avg":
            partes.append(f"COALESCE(AVG({expr}), 0) AS {alias_col}")
        else:
            partes.append(f"COALESCE(SUM({expr}), 0) AS {alias_col}")
    return ", ".join(partes)


def metadata_poligono_sql(geom_expr: str) -> str:
    """Expresión SQL (CTE poly) para centroide, bbox y vértices en WGS84."""
    return f"""
        ST_AsGeoJSON(ST_Centroid({geom_expr}), 6) AS centroide_json,
        ST_XMin({geom_expr}) AS xmin,
        ST_YMin({geom_expr}) AS ymin,
        ST_XMax({geom_expr}) AS xmax,
        ST_YMax({geom_expr}) AS ymax,
        ST_NPoints({geom_expr}) AS n_vertices,
        ST_AsGeoJSON({geom_expr}, 6) AS geom_wgs84_json,
        ST_Area({geom_expr}::geography) AS area_m2
    """


def _etiqueta_localidad(
    nom_loc: Optional[str],
    nom_mun: Optional[str],
    cvegeo: Optional[str],
) -> str:
    """Nombre legible para listados en análisis ITER."""
    nombre = (nom_loc or "").strip() or (cvegeo or "").strip() or "Localidad"
    mun = (nom_mun or "").strip()
    if mun and mun.lower() not in nombre.lower():
        return f"{nombre} ({mun})"
    return nombre


def _sql_filtro_interseccion_poligono(geom_alias: str, geom_col: str = "the_geom") -> List[str]:
    q_geom = quote_ident(geom_col)
    return [
        f"{geom_alias}.{q_geom} IS NOT NULL",
        f"ST_Transform({geom_alias}.{q_geom}, 4326) && poly.geom4326",
        (
            f"ST_Intersects("
            f"ST_MakeValid(ST_Transform({geom_alias}.{q_geom}, 4326)), poly.geom4326)"
        ),
    ]


def _listar_localidades_iter_poligono(
    conn,
    poly_sql: str,
    params: Dict[str, Any],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """
    Localidades puntuales dentro del polígono, separadas por presencia de fila en atlas.iter.
    """
    q_geom_tbl = qualified(T_LOC_PUNTO)
    q_iter = qualified(T_ITER)
    q_geom = quote_ident("the_geom")
    q_join = quote_ident("cvegeo")
    intersect = " AND ".join(_sql_filtro_interseccion_poligono("loc"))

    sql = f"""
        WITH poly AS (
            {poly_sql}
        ),
        geo_hits AS (
            SELECT DISTINCT
                   TRIM(loc.{q_join}::text) AS cvegeo,
                   NULLIF(TRIM(loc.nom_loc::text), '') AS nom_loc,
                   NULLIF(TRIM(loc.nom_mun::text), '') AS nom_mun
              FROM {q_geom_tbl} loc
             CROSS JOIN poly
             WHERE {intersect}
        )
        SELECT g.cvegeo,
               g.nom_loc,
               g.nom_mun,
               EXISTS (
                   SELECT 1
                     FROM {q_iter} d
                    WHERE TRIM(d.{q_join}::text) = g.cvegeo
               ) AS tiene_iter
          FROM geo_hits g
         ORDER BY g.nom_loc NULLS LAST, g.cvegeo
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    con_datos: List[Dict[str, str]] = []
    sin_datos: List[Dict[str, str]] = []
    seen_con: set[str] = set()
    seen_sin: set[str] = set()

    for row in rows:
        cvegeo = (row.get("cvegeo") or "").strip()
        if not cvegeo:
            continue
        etiqueta = _etiqueta_localidad(row.get("nom_loc"), row.get("nom_mun"), cvegeo)
        item = {
            "cvegeo": cvegeo,
            "nombre": (row.get("nom_loc") or "").strip() or cvegeo,
            "etiqueta": etiqueta,
        }
        if row.get("tiene_iter"):
            if cvegeo not in seen_con:
                seen_con.add(cvegeo)
                con_datos.append(item)
        elif cvegeo not in seen_sin:
            seen_sin.add(cvegeo)
            sin_datos.append(item)

    return con_datos, sin_datos


def _ejecutar_conteo_puntos(
    conn,
    *,
    meta: Mapping[str, Any],
    geojson: Any,
    cve_mun: Optional[str] = None,
) -> Dict[str, Any]:
    """Cuenta puntos (DENUE, CLUES, etc.) dentro del polígono."""
    poly_sql, params = _sql_cte_poligono(geojson)
    cve = norm_cve_mun(cve_mun) if cve_mun else None
    if cve:
        params = {**params, "cve_mun": cve}

    alias = "pt"
    q_tbl = qualified(meta["tabla"])
    where_sql = _sql_filtros_capa_conteo(meta, alias, cve)
    meta_sql = metadata_poligono_sql("geom4326")

    sql = f"""
        WITH poly AS (
            {poly_sql}
        ),
        meta AS (
            SELECT {meta_sql}
              FROM poly
        ),
        agg AS (
            SELECT COUNT(*)::bigint AS registros_intersectados
              FROM {q_tbl} {alias}
             CROSS JOIN poly
             WHERE {where_sql}
        )
        SELECT agg.*, meta.*
          FROM agg
         CROSS JOIN meta
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()

    if not row:
        raise RuntimeError("SIN_RESULTADO")

    poligono = {
        "centroide": None,
        "bbox": None,
        "vertices": int(row.get("n_vertices") or 0),
        "area_m2": float(row["area_m2"]) if row.get("area_m2") is not None else None,
        "coordenadas": None,
    }

    n = int(row.get("registros_intersectados") or 0)
    etiqueta = meta["etiqueta"]
    return {
        "ok": True,
        "modo": "conteo",
        "tabla": meta["tabla"],
        "capa_id": meta["id"],
        "capa_etiqueta": etiqueta,
        "grupo": meta.get("grupo"),
        "registros_intersectados": n,
        "poligono": poligono,
        "totales": {"total": n},
        "campos": [
            {
                "columna": "total",
                "etiqueta": etiqueta,
                "agregacion": "count",
                "valor": n,
            }
        ],
        "cve_mun": cve,
    }


def detectar_capas_intersectantes(
    conn,
    *,
    geojson: Any,
    cve_mun: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Devuelve capas de análisis con al menos un registro intersectando el polígono.
  """
    poly_sql, params = _sql_cte_poligono(geojson)
    cve = norm_cve_mun(cve_mun) if cve_mun else None
    if cve:
        params = {**params, "cve_mun": cve}

    q_inv = qualified(T_C_INV)
    q_loc = qualified(T_LOC_PUNTO)
    mun_inv = _sql_mun_filter("inv", cve)
    mun_iter = _sql_mun_filter("loc", cve)
    inv_exists = " AND ".join(_sql_filtro_interseccion_poligono("inv", "the_geom"))
    iter_exists = " AND ".join(_sql_filtro_interseccion_poligono("loc", "the_geom"))

    q_denue = qualified(T_DENUE)
    q_clues = qualified(T_CLUES)
    denue_intersect = " AND ".join(_sql_filtro_interseccion_poligono("pt", "the_geom"))
    clues_intersect = " AND ".join(_sql_filtro_interseccion_poligono("cl", "the_geom"))
    mun_denue = _sql_mun_filter("pt", cve)
    mun_clues = _sql_mun_filter("cl", cve)

    cte_chunks = [
        f"""
        poly AS (
            {poly_sql}
        )""",
        f"""
        inv_hit AS (
            SELECT EXISTS (
                SELECT 1
                  FROM {q_inv} inv
                 CROSS JOIN poly
                 WHERE {inv_exists}{mun_inv}
                 LIMIT 1
            ) AS hit
        )""",
        f"""
        iter_hit AS (
            SELECT EXISTS (
                SELECT 1
                  FROM {q_loc} loc
                 CROSS JOIN poly
                 WHERE {iter_exists}{mun_iter}
                 LIMIT 1
            ) AS hit
        )""",
        f"""
        clues_hit AS (
            SELECT EXISTS (
                SELECT 1
                  FROM {q_clues} cl
                 CROSS JOIN poly
                 WHERE {clues_intersect}{mun_clues}
                 LIMIT 1
            ) AS hit
        )""",
        f"""
        denue_codes AS (
            SELECT COALESCE(
                ARRAY(
                    SELECT DISTINCT TRIM(pt.codigo_act::text)
                      FROM {q_denue} pt
                     CROSS JOIN poly
                     WHERE {denue_intersect}{mun_denue}
                       AND pt.codigo_act IS NOT NULL
                       AND TRIM(pt.codigo_act::text) <> ''
                ),
                ARRAY[]::text[]
            ) AS codes
        )""",
    ]
    select_cols = [
        "(SELECT hit FROM inv_hit) AS inv_ok",
        "(SELECT hit FROM iter_hit) AS iter_ok",
        "(SELECT hit FROM clues_hit) AS clues_ok",
        "(SELECT codes FROM denue_codes) AS denue_codes",
    ]

    sql = f"""
        WITH {",".join(cte_chunks)}
        SELECT {", ".join(select_cols)}
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone() or {}

    capas: List[Dict[str, Any]] = []
    orden_grupo = {"censales": 0, "denue": 1, "salud": 2}

    if row.get("inv_ok"):
        capas.append(_capa_intersect_resumen(CAPAS_ANALISIS["c_inv"]))
    if row.get("iter_ok"):
        capas.append(_capa_intersect_resumen(CAPAS_ANALISIS["iter"]))
    if row.get("clues_ok"):
        capas.append(_capa_intersect_resumen(CAPAS_ANALISIS["clues"]))

    denue_codes_raw = row.get("denue_codes") or []
    if isinstance(denue_codes_raw, str):
        denue_codes_raw = [
            p.strip() for p in denue_codes_raw.strip("{}").split(",") if p.strip()
        ]
    found_codes = {str(c).strip() for c in denue_codes_raw if str(c).strip()}

    for meta in CAPAS_ANALISIS.values():
        if meta.get("grupo") != "denue":
            continue
        codes = {str(c) for c in meta.get("codigo_act", [])}
        if codes & found_codes:
            capas.append(_capa_intersect_resumen(meta))

    capas.sort(key=lambda c: (orden_grupo.get(c.get("grupo", ""), 9), c.get("etiqueta", "")))
    return capas


def _capa_intersect_resumen(meta: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": meta["id"],
        "tabla": meta["tabla"],
        "etiqueta": meta["etiqueta"],
        "descripcion": meta.get("descripcion", ""),
        "grupo": meta.get("grupo", "otros"),
        "modo": meta.get("modo", "agregacion"),
    }


def ejecutar_analisis_espacial(
    conn,
    *,
    nombre_tabla: str,
    campos_elegidos: Sequence[str],
    geojson: Any,
    cve_mun: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Agrega campos numéricos de ``tabla`` intersectando ``geojson`` (EPSG:4326).

    Returns:
        Dict listo para JSON con polígono (coordenadas), totales y metadatos.
    """
    meta = _resolver_meta_tabla(nombre_tabla)
    if _es_capa_conteo(meta):
        return _ejecutar_conteo_puntos(conn, meta=meta, geojson=geojson, cve_mun=cve_mun)

    if not campos_elegidos:
        raise ValueError("SIN_CAMPOS")

    tabla = meta["tabla"]
    geom_col = meta["geom_column"]
    geom_tabla = meta.get("geom_tabla")
    join_col = (meta.get("join_column") or "cvegeo").lower()

    poly_sql, params = _sql_cte_poligono(geojson)

    columnas_meta, columnas = _validar_campos_solicitados(conn, meta["id"], campos_elegidos)

    if geom_tabla:
        q_geom_tbl = qualified(geom_tabla)
        q_data_tbl = qualified(tabla)
        q_join = quote_ident(join_col)
        data_alias = "dat"
        select_agg = _sql_agregaciones(columnas_meta, data_alias)
        from_sql = (
            f"{q_geom_tbl} loc "
            f"INNER JOIN {q_data_tbl} {data_alias} "
            f"ON TRIM(loc.{q_join}::text) = TRIM({data_alias}.{q_join}::text)"
        )
        filtros = _sql_filtro_interseccion_poligono("loc", geom_col)
    else:
        data_alias = "t"
        select_agg = _sql_agregaciones(columnas_meta, data_alias)
        from_sql = f"{qualified(tabla)} {data_alias}"
        filtros = _sql_filtro_interseccion_poligono(data_alias, geom_col)

    where_sql = " AND ".join(filtros)
    meta_sql = metadata_poligono_sql("geom4326")

    sql = f"""
        WITH poly AS (
            {poly_sql}
        ),
        meta AS (
            SELECT {meta_sql}
              FROM poly
        ),
        agg AS (
            SELECT COUNT(*)::bigint AS registros_intersectados,
                   {select_agg}
              FROM {from_sql}
             CROSS JOIN poly
             WHERE {where_sql}
        )
        SELECT agg.*, meta.*
          FROM agg
         CROSS JOIN meta
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()

    if not row:
        raise RuntimeError("SIN_RESULTADO")

    # --- Metadatos del polígono (coordenadas) ---
    centroide = None
    try:
        cj = (row.get("centroide_json") or "").strip()
        if cj:
            cg = json.loads(cj)
            if cg.get("type") == "Point":
                coords = cg.get("coordinates") or []
                if len(coords) >= 2:
                    centroide = {"lon": float(coords[0]), "lat": float(coords[1])}
    except (json.JSONDecodeError, TypeError, ValueError):
        centroide = None

    coordenadas = None
    try:
        gj = (row.get("geom_wgs84_json") or "").strip()
        if gj:
            geom_obj = json.loads(gj)
            if geom_obj.get("type") == "Polygon":
                coordenadas = geom_obj.get("coordinates")
            elif geom_obj.get("type") == "MultiPolygon":
                coordenadas = geom_obj.get("coordinates")
    except (json.JSONDecodeError, TypeError, ValueError):
        coordenadas = None

    bbox = None
    if row.get("xmin") is not None:
        bbox = [
            float(row["xmin"]),
            float(row["ymin"]),
            float(row["xmax"]),
            float(row["ymax"]),
        ]

    poligono = {
        "centroide": centroide,
        "bbox": bbox,
        "vertices": int(row.get("n_vertices") or 0),
        "area_m2": float(row["area_m2"]) if row.get("area_m2") is not None else None,
        "coordenadas": coordenadas,
    }

    totales: Dict[str, Any] = {}
    campos_resp: List[Dict[str, Any]] = []
    for col in columnas_meta:
        nombre = col["columna"]
        valor = row.get(nombre)
        if valor is not None and hasattr(valor, "__float__"):
            valor = float(valor)
        totales[nombre] = valor
        campos_resp.append(
            {
                "columna": nombre,
                "etiqueta": col["etiqueta"],
                "agregacion": col["agregacion"],
                "valor": valor,
            }
        )

    resultado: Dict[str, Any] = {
        "ok": True,
        "tabla": tabla,
        "capa_id": meta["id"],
        "capa_etiqueta": meta["etiqueta"],
        "registros_intersectados": int(row.get("registros_intersectados") or 0),
        "poligono": poligono,
        "totales": totales,
        "campos": campos_resp,
        "cve_mun": norm_cve_mun(cve_mun) or None,
    }

    if meta["id"] == "iter":
        con_datos, sin_datos = _listar_localidades_iter_poligono(conn, poly_sql, params)
        resultado["localidades_con_datos"] = con_datos
        resultado["localidades_sin_datos"] = sin_datos

    return resultado
