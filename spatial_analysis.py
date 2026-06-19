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

from tables import SCHEMA, T_C_INV, qualified
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
    },
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
    return [
        {
            "id": meta["id"],
            "tabla": meta["tabla"],
            "etiqueta": meta["etiqueta"],
            "descripcion": meta.get("descripcion", ""),
        }
        for meta in CAPAS_ANALISIS.values()
    ]


def _resolver_meta_tabla(nombre_tabla: str) -> Dict[str, Any]:
    clave = (nombre_tabla or "").strip().lower()
    if clave not in CAPAS_ANALISIS:
        raise ValueError("TABLA_NO_PERMITIDA")
    return CAPAS_ANALISIS[clave]


def _etiqueta_columna(tabla: str, columna: str) -> str:
    lc = columna.lower()
    if tabla == T_C_INV and lc in _INV_CAMPOS_MAP:
        return _INV_CAMPOS_MAP[lc]["etiqueta"]
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
    if meta["id"] == "c_inv":
        return _columnas_inv_analisis()

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


def _sql_expr_columna(col: Mapping[str, str]) -> str:
    """Expresión SQL segura para agregar (incluye varchar → numeric en INV)."""
    qn = quote_ident(col["columna"])
    if col.get("cast") == "text_numeric":
        cleaned = (
            f"NULLIF(regexp_replace(TRIM({qn}::text), '[^0-9\\.-]', '', 'g'), '')"
        )
        return (
            f"CASE WHEN {cleaned} ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            f"THEN {cleaned}::numeric END"
        )
    return qn


def _sql_agregaciones(meta: Mapping[str, Any], columnas_meta: Sequence[Dict[str, str]]) -> str:
    """Construye la lista SUM(col) / AVG(col) validada."""
    partes: List[str] = []
    for col in columnas_meta:
        nombre = col["columna"]
        expr = _sql_expr_columna(col)
        alias = quote_ident(nombre)
        if col["agregacion"] == "avg":
            partes.append(f"COALESCE(AVG({expr}), 0) AS {alias}")
        else:
            partes.append(f"COALESCE(SUM({expr}), 0) AS {alias}")
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
    tabla = meta["tabla"]
    geom_col = meta["geom_column"]

    poly_sql, params = _sql_cte_poligono(geojson)

    columnas_meta, columnas = _validar_campos_solicitados(conn, meta["id"], campos_elegidos)
    select_agg = _sql_agregaciones(meta, columnas_meta)
    q_geom = quote_ident(geom_col)
    q_tabla = qualified(tabla)

    # El polígono dibujado define el ámbito; no filtrar por cve_mun del sidebar
    # (el usuario puede dibujar en cualquier zona visible del mapa).
    filtros = [
        f"{q_geom} IS NOT NULL",
        f"ST_Transform({q_geom}, 4326) && poly.geom4326",
        (
            f"ST_Intersects("
            f"ST_MakeValid(ST_Transform({q_geom}, 4326)), poly.geom4326)"
        ),
    ]

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
              FROM {q_tabla} t
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

    return {
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
