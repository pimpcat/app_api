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
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

from tables import SCHEMA, T_C_INV, T_DENUE, T_ITER, T_LOC_PUNTO, qualified
from utils import mun_where_sql, norm_cve_mun, quote_ident
from visor_analysis_loader import inv_campos_analisis, iter_campos_analisis, _flat_fields_from_sections
from visor_attribute_filter import attribute_filter_where_sql
from visor_catalog_loader import merge_capas_analisis
from visor_tabular import (
    list_clues_detail_rows,
    list_denue_detail_rows,
)

# ---------------------------------------------------------------------------
# Catálogo de capas habilitadas para análisis espacial (data-driven).
# INV/ITER: config/visor/analysis_catalog.json
# Studio / DENUE / CLUES: config/visor/catalog.json
# ---------------------------------------------------------------------------
def capas_analisis() -> Dict[str, Dict[str, Any]]:
    return merge_capas_analisis()


CAPAS_ANALISIS: Dict[str, Dict[str, Any]] = capas_analisis()


def refresh_capas_analisis() -> Dict[str, Dict[str, Any]]:
    global CAPAS_ANALISIS
    CAPAS_ANALISIS = merge_capas_analisis()
    return CAPAS_ANALISIS

# Campos permitidos para análisis espacial sobre INV 2020 (población → vivienda).
INV_CAMPOS_ANALISIS: List[Dict[str, str]] = inv_campos_analisis()

_INV_CAMPOS_MAP: Dict[str, Dict[str, str]] = {c["columna"]: c for c in INV_CAMPOS_ANALISIS}

# Campos ITER 2020 (atlas.iter, geometría vía atlas.c_loc_punto.cvegeo).
ITER_CAMPOS_ANALISIS: List[Dict[str, str]] = iter_campos_analisis()

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
    orden_grupo = {"censales": 0, "denue": 1, "salud": 2, "tematicas": 3}
    items = [
        {
            "id": meta["id"],
            "tabla": meta["tabla"],
            "etiqueta": meta["etiqueta"],
            "descripcion": meta.get("descripcion", ""),
            "grupo": meta.get("grupo", "otros"),
            "modo": meta.get("modo", "agregacion"),
        }
        for meta in capas_analisis().values()
    ]
    items.sort(key=lambda c: (orden_grupo.get(c["grupo"], 9), c["etiqueta"]))
    return items


def _es_capa_conteo(meta: Mapping[str, Any]) -> bool:
    return meta.get("modo") == "conteo"


def _mun_filter_respects_cvegeo(meta: Mapping[str, Any]) -> bool:
    return meta.get("mun_filter_cvegeo") is not False


def _sql_mun_filter_clause(meta: Mapping[str, Any], alias: str, cve: Optional[str]) -> str:
    """Filtro municipal alineado con el mapa/tabular (mun_where_sql)."""
    if not cve:
        return ""
    mun_f = meta.get("mun_filter", "cve_mun")
    if mun_f is False:
        return ""
    return mun_where_sql(alias, with_cvegeo=_mun_filter_respects_cvegeo(meta))


def _with_mun_cve_param(params: Dict[str, Any], cve: Optional[str]) -> Dict[str, Any]:
    if not cve:
        return params
    return {**params, "cve": cve}


def _sql_codigo_act_filter(alias: str, codes: Sequence[int]) -> str:
    """Filtro codigo_act (varchar en c_denue) — normaliza dígitos como el visor MVT."""
    safe = [str(int(c)) for c in codes if str(c).strip().isdigit()]
    if not safe:
        return "FALSE"
    col = f"regexp_replace(TRIM({alias}.codigo_act::text), '[^0-9]', '', 'g')"
    in_list = ", ".join(f"'{c}'" for c in safe)
    return f"({col} IN ({in_list}))"


def _sql_filtros_capa_meta(meta: Mapping[str, Any], alias: str, cve: Optional[str]) -> List[str]:
    parts = list(_sql_filtro_interseccion_poligono(alias, meta.get("geom_column", "the_geom")))
    codes = meta.get("codigo_act")
    if codes:
        parts.append(_sql_codigo_act_filter(alias, codes))
    attr_f = meta.get("attribute_filter")
    if attr_f:
        attr_sql = attribute_filter_where_sql(attr_f, alias=alias)
        if attr_sql:
            parts.append(attr_sql)
    mun_sql = _sql_mun_filter_clause(meta, alias, cve)
    if mun_sql:
        parts.append(f"({mun_sql})")
    return parts


def _sql_filtros_capa_conteo(meta: Mapping[str, Any], alias: str, cve: Optional[str]) -> str:
    return " AND ".join(_sql_filtros_capa_meta(meta, alias, cve))


def _from_sql_capa(meta: Mapping[str, Any]) -> Tuple[str, str, str]:
    """
    Devuelve (from_sql, geom_alias, data_alias) para consultas espaciales.
    geom_alias se usa en filtros de intersección; data_alias en agregaciones/atributos.
    """
    geom_tabla = meta.get("geom_tabla")
    join_col = (meta.get("join_column") or "cvegeo").lower()
    tabla = meta["tabla"]
    if geom_tabla:
        q_geom = qualified(geom_tabla)
        q_data = qualified(tabla)
        geom_alias = "loc"
        data_alias = "dat"
        from_sql = (
            f"{q_geom} {geom_alias} "
            f"INNER JOIN {q_data} {data_alias} "
            f"ON TRIM({geom_alias}.{quote_ident(join_col)}::text) = "
            f"TRIM({data_alias}.{quote_ident(join_col)}::text)"
        )
        return from_sql, geom_alias, data_alias
    data_alias = "t"
    return f"{qualified(tabla)} {data_alias}", data_alias, data_alias


def _sql_exists_capa(meta: Mapping[str, Any], cve: Optional[str]) -> str:
    from_sql, geom_alias, data_alias = _from_sql_capa(meta)
    filtros = _sql_filtros_capa_meta(meta, geom_alias, cve)
    where_sql = " AND ".join(filtros)
    return f"""
        SELECT 1
          FROM {from_sql}
         CROSS JOIN poly
         WHERE {where_sql}
         LIMIT 1
    """


def _list_generic_detail_rows(
    conn,
    *,
    meta: Mapping[str, Any],
    detail_columns: Sequence[Mapping[str, Any]],
    where_sql: str,
    params: Mapping[str, Any],
    from_sql: str,
    with_clause: Optional[str],
    alias: Optional[str] = None,
) -> Dict[str, Any]:
    from visor_tabular import _build_select_parts, _columns_with_numero, _execute_detail_query, _rows_with_numero

    if alias:
        row_alias = alias
    elif meta.get("geom_tabla"):
        row_alias = "dat"
    else:
        row_alias = "pt"
    columns: List[Dict[str, str]] = []
    for col in detail_columns:
        if not isinstance(col, Mapping):
            continue
        field = str(col.get("columna") or col.get("column") or col.get("field") or "").strip().lower()
        if not field or not _CAMPO_RE.match(field):
            continue
        label = str(col.get("etiqueta") or col.get("label") or field).strip() or field
        columns.append({"field": field, "sql": field, "label": label})
    if not columns:
        return {"columns": [], "rows": [], "filas_truncadas": False}

    select_parts = _build_select_parts(columns, row_alias)
    order_gid = next((c["sql"] for c in columns if c["field"] == "gid"), None) or columns[0]["sql"]
    order_by = f"{row_alias}.{quote_ident(order_gid)} ASC"
    rows, truncated = _execute_detail_query(
        conn,
        columns=columns,
        select_parts=select_parts,
        from_sql=from_sql,
        where_sql=where_sql,
        params=params,
        order_by=order_by,
        with_clause=with_clause,
    )
    return {
        "columns": _columns_with_numero(columns),
        "rows": _rows_with_numero(rows),
        "filas_truncadas": truncated,
    }


def _resolver_meta_tabla(nombre_tabla: str) -> Dict[str, Any]:
    clave = (nombre_tabla or "").strip().lower()
    capas = capas_analisis()
    if clave not in capas:
        raise ValueError("TABLA_NO_PERMITIDA")
    return capas[clave]


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


def _columnas_desde_sections(sections: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for field in _flat_fields_from_sections(sections):
        item: Dict[str, str] = {
            "columna": field["columna"],
            "tipo": "character varying",
            "agregacion": field.get("agregacion") or "sum",
            "etiqueta": field.get("etiqueta") or field["columna"],
        }
        item["cast"] = "text_numeric"
        out.append(item)
    return out


def listar_columnas_numericas(conn, nombre_tabla: str) -> List[Dict[str, str]]:
    """
    Columnas disponibles para el selector del análisis espacial.

    Prioridad: sections del catálogo → catálogo fijo INV/ITER → information_schema.
    """
    meta = _resolver_meta_tabla(nombre_tabla)
    if _es_capa_conteo(meta):
        return []
    sections = meta.get("sections")
    if sections:
        return _columnas_desde_sections(sections)
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
    params = _with_mun_cve_param(params, cve)

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

    detail: Dict[str, Any] = {"columns": [], "rows": [], "filas_truncadas": False}
    show_detail = bool(
        meta.get("detail_table")
        or meta["id"] == "clues"
        or meta.get("codigo_act")
    )
    if n > 0 and show_detail:
        try:
            detail_alias = "pt"
            q_tbl = qualified(meta["tabla"])
            where_parts = _sql_filtros_capa_meta(meta, detail_alias, cve)
            where_detail = " AND ".join(where_parts)
            poly_cte = f"poly AS (\n            {poly_sql}\n        )"
            from_pt = f"{q_tbl} {detail_alias} CROSS JOIN poly"
            if meta["id"] == "clues":
                detail = list_clues_detail_rows(
                    conn,
                    where_sql=where_detail,
                    params=params,
                    from_sql=from_pt,
                    with_clause=poly_cte,
                )
            elif meta.get("codigo_act"):
                detail = list_denue_detail_rows(
                    conn,
                    codigo_act=meta["codigo_act"],
                    where_sql=where_detail,
                    params=params,
                    from_sql=from_pt,
                    with_clause=poly_cte,
                    apply_codigo_filter=False,
                )
            elif meta.get("detail_columns"):
                detail = _list_generic_detail_rows(
                    conn,
                    meta=meta,
                    detail_columns=meta["detail_columns"],
                    where_sql=where_detail,
                    params=params,
                    from_sql=from_pt,
                    with_clause=poly_cte,
                    alias=detail_alias,
                )
        except Exception as exc:
            logger.warning(
                "Tabla detalle análisis espacial (%s): %s",
                meta.get("id"),
                exc,
            )
            detail = {"columns": [], "rows": [], "filas_truncadas": False}

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
        "columns": detail.get("columns") or [],
        "rows": detail.get("rows") or [],
        "filas_truncadas": bool(detail.get("filas_truncadas")),
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
    params = _with_mun_cve_param(params, cve)

    capas_map = capas_analisis()
    denue_metas = [m for m in capas_map.values() if m.get("grupo") == "denue"]
    other_metas = [m for m in capas_map.values() if m.get("grupo") != "denue"]

    cte_chunks = [
        f"""
        poly AS (
            {poly_sql}
        )"""
    ]
    select_cols: List[str] = []

    for meta in other_metas:
        capa_id = str(meta["id"]).replace("-", "_")
        cte_chunks.append(
            f"""
        hit_{capa_id} AS (
            SELECT EXISTS (
                {_sql_exists_capa(meta, cve)}
            ) AS hit
        )"""
        )
        select_cols.append(f"(SELECT hit FROM hit_{capa_id}) AS hit_{capa_id}")

    q_denue = qualified(T_DENUE)
    denue_intersect = " AND ".join(_sql_filtro_interseccion_poligono("pt", "the_geom"))
    mun_denue = f" AND ({mun_where_sql('pt', with_cvegeo=False)})" if cve else ""
    codigo_act_norm = "regexp_replace(TRIM(pt.codigo_act::text), '[^0-9]', '', 'g')"
    cte_chunks.append(
        f"""
        denue_codes AS (
            SELECT COALESCE(
                ARRAY(
                    SELECT DISTINCT {codigo_act_norm}
                      FROM {q_denue} pt
                     CROSS JOIN poly
                     WHERE {denue_intersect}{mun_denue}
                       AND pt.codigo_act IS NOT NULL
                       AND {codigo_act_norm} <> ''
                ),
                ARRAY[]::text[]
            ) AS codes
        )"""
    )
    select_cols.append("(SELECT codes FROM denue_codes) AS denue_codes")

    sql = f"""
        WITH {",".join(cte_chunks)}
        SELECT {", ".join(select_cols)}
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone() or {}

    capas: List[Dict[str, Any]] = []
    orden_grupo = {"censales": 0, "denue": 1, "salud": 2, "tematicas": 3}

    for meta in other_metas:
        capa_id = str(meta["id"]).replace("-", "_")
        if row.get(f"hit_{capa_id}"):
            capas.append(_capa_intersect_resumen(meta))

    denue_codes_raw = row.get("denue_codes") or []
    if isinstance(denue_codes_raw, str):
        denue_codes_raw = [
            p.strip() for p in denue_codes_raw.strip("{}").split(",") if p.strip()
        ]
    found_codes = {str(c).strip() for c in denue_codes_raw if str(c).strip()}

    for meta in denue_metas:
        codes = {str(int(c)) for c in meta.get("codigo_act", []) if str(c).strip().isdigit()}
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

    poly_sql, params = _sql_cte_poligono(geojson)
    cve_norm = norm_cve_mun(cve_mun) if cve_mun else None
    params = _with_mun_cve_param(params, cve_norm)

    columnas_meta, _columnas = _validar_campos_solicitados(conn, meta["id"], campos_elegidos)

    from_sql, geom_alias, data_alias = _from_sql_capa(meta)
    select_agg = _sql_agregaciones(columnas_meta, data_alias)
    filtros = _sql_filtros_capa_meta(meta, geom_alias, cve_norm)
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

    n = int(row.get("registros_intersectados") or 0)
    if meta.get("detail_table") and meta.get("detail_columns") and n > 0:
        from_sql_capa, geom_alias_d, data_alias_d = _from_sql_capa(meta)
        where_detail = " AND ".join(_sql_filtros_capa_meta(meta, geom_alias_d, cve_norm))
        poly_cte = f"poly AS (\n            {poly_sql}\n        )"
        detail = _list_generic_detail_rows(
            conn,
            meta=meta,
            detail_columns=meta["detail_columns"],
            where_sql=where_detail,
            params=params,
            from_sql=f"{from_sql_capa} CROSS JOIN poly",
            with_clause=poly_cte,
            alias=data_alias_d,
        )
        resultado["columns"] = detail.get("columns") or []
        resultado["rows"] = detail.get("rows") or []
        resultado["filas_truncadas"] = bool(detail.get("filas_truncadas"))

    return resultado
