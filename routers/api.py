"""Endpoints REST del Atlas (equivalente a api/*.php)."""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from column_resolver import resolve_column
from config import get_settings
from database import get_db
from explorador import build_explorador_all_response, build_explorador_response
from ranking import build_top_bottom_response
from tab_municipal import fetch_nacional_estatal_municipio, load_tab_municipal_rows
from tables import SCHEMA, T_COL_ASE, T_CONTEXTO, T_L, T_MUN, T_TAB_MUNICIPAL, T_TAB_NACIONAL, T_CLUES, qualified
from utils import is_mun_cve3, mun_where_sql, norm_cve_mun, quote_ident, row_numeric
from vistas_educacion import build_analfabetismo_response, build_escolaridad_response
from vistas_tab_municipal import (
    build_caracteristicas_economicas_response,
    build_instituciones_admin_response,
    build_inversion_publica_response,
    build_poblacion_ocupada_response,
    build_superficie_agricultura_response,
    build_unidades_medicas_response,
    build_vivienda_participacion_response,
)
from vistas_nacional import ent_key_to_int
from visor_export import export_error_message, export_layer
from visor_layers import layer_catalog
from visor_tabular import (
    build_tabular_xlsx,
    fetch_tabular_data,
    list_tabular_layers,
    tabular_error_message,
)
from visor_buffer import buffer_geometry_geojson, fetch_feature_geometry_geojson, fetch_feature_outline_geojson
from geocoder import buscar_lugares
from spatial_analysis import (
    ejecutar_analisis_espacial,
    listar_capas_disponibles,
    listar_columnas_numericas,
)

router = APIRouter()

INEGI_WMTS_LAYER = "MapaBaseTopograficov61_sinsombreado"


def _martin_catalog_layer_ids(catalog: Any) -> List[str]:
    """IDs de capas en catálogo Martin (dict legacy o lista con campo id)."""
    ids: List[str] = []
    if isinstance(catalog, dict):
        tiles = catalog.get("tiles")
        if isinstance(tiles, dict):
            ids.extend(str(k) for k in tiles.keys())
        else:
            ids.extend(str(k) for k in catalog.keys() if k not in ("tiles", "sprites", "fonts"))
    elif isinstance(catalog, list):
        for item in catalog:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
    return ids


def _martin_catalog_has_layer(catalog: Any, layer_id: str) -> bool:
    return layer_id in _martin_catalog_layer_ids(catalog)
INEGI_WMTS_UPSTREAM = (
    "https://gaiamapas.inegi.org.mx/mdmCache/service/wmts"
    "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
    f"&LAYER={INEGI_WMTS_LAYER}"
    "&STYLE=default&FORMAT=image/jpeg&TILEMATRIXSET=EPSG:3857"
)


def _fetch_inegi_wmts_tile(z: int, x: int, y: int) -> bytes:
    url = f"{INEGI_WMTS_UPSTREAM}&TILEMATRIX=EPSG:900913:{z}&TILEROW={y}&TILECOL={x}"
    req = urllib.request.Request(url, headers={"User-Agent": "AtlasGro/2.0 (tile-proxy)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"INEGI_HTTP_{exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail="INEGI_UNREACHABLE") from exc
    if not data or data[:2] != b"\xff\xd8":
        raise HTTPException(status_code=502, detail="INEGI_INVALID_TILE")
    return data


def _sel_params(cve_mun: Optional[str], nom_mun: Optional[str]):
    cve = norm_cve_mun(cve_mun or "")
    nom = (nom_mun or "").strip().lower()
    return cve, nom


# --- Infra ---

@router.get("/health")
@router.get("/api/health")
@router.get("/api/health.php")
def health():
    settings = get_settings()
    payload: Dict[str, Any] = {
        "ok": True,
        "service": "atlasgro-api",
        "time": datetime.now(timezone.utc).isoformat(),
        "database": settings.get("database_name") or None,
        "schema": settings.get("schema") or None,
    }
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database() AS db, current_schema() AS sch")
                row = cur.fetchone() or {}
                payload["database_connected"] = row.get("db")
                payload["search_path_schema"] = row.get("sch")
                cur.execute(
                    f"""
                    SELECT COUNT(*)::int AS total,
                           COUNT(the_geom)::int AS con_geom,
                           COUNT(DISTINCT cve_mun)::int AS muns
                      FROM {qualified(T_CLUES)}
                    """
                )
                clues = cur.fetchone() or {}
                cur.execute(
                    """
                    SELECT type, srid
                      FROM geometry_columns
                     WHERE f_table_schema = %s AND f_table_name = %s
                    """,
                    (SCHEMA, T_CLUES),
                )
                geom = cur.fetchone() or {}
                payload["c_clues"] = {
                    "table": f"{SCHEMA}.{T_CLUES}",
                    "rows": clues.get("total"),
                    "with_geom": clues.get("con_geom"),
                    "municipios": clues.get("muns"),
                    "geom_type": geom.get("type"),
                    "srid": geom.get("srid"),
                }
                cur.execute(
                    f"""
                    SELECT TRIM(cve_mun::text) AS cve_mun, COUNT(*)::int AS n
                      FROM {qualified(T_CLUES)}
                     WHERE cve_mun IS NOT NULL AND TRIM(cve_mun::text) <> ''
                     GROUP BY 1
                     ORDER BY n DESC
                     LIMIT 5
                    """
                )
                payload["c_clues"]["sample_cve_mun"] = cur.fetchall()
        try:
            req = urllib.request.Request(
                "http://martin:3000/catalog",
                headers={"User-Agent": "AtlasGro/2.0 (health)"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                catalog = json.loads(resp.read().decode("utf-8"))
            layer_ids = _martin_catalog_layer_ids(catalog)
            payload["martin_tiles"] = sorted(layer_ids)
            payload["martin_has_c_clues"] = _martin_catalog_has_layer(catalog, T_CLUES)
            try:
                tj_req = urllib.request.Request(
                    f"http://martin:3000/{T_CLUES}",
                    headers={"User-Agent": "AtlasGro/2.0 (health)"},
                )
                with urllib.request.urlopen(tj_req, timeout=5) as tj_resp:
                    tilejson = json.loads(tj_resp.read().decode("utf-8"))
                layers = tilejson.get("vector_layers") or []
                for layer in layers:
                    if layer.get("id") == T_CLUES:
                        payload["c_clues_mvt_fields"] = (
                            layer.get("fields")
                            or layer.get("properties")
                            or layer.get("attributes")
                        )
                        break
                if payload.get("c_clues_mvt_fields") is None and layers:
                    payload["c_clues_mvt_layer_sample"] = layers[0]
            except Exception as tj_exc:
                payload["c_clues_tilejson_error"] = str(tj_exc).split("\n", 1)[0]
        except Exception as martin_exc:
            payload["martin_catalog_error"] = str(martin_exc).split("\n", 1)[0]
    except Exception as exc:
        payload["ok"] = False
        payload["database_error"] = str(exc).split("\n", 1)[0]
    return payload


@router.get("/inegi/wmts/tile")
@router.get("/api/inegi/wmts/tile")
@router.get("/api/inegi_wmts_tile.php")
def inegi_wmts_tile(
    z: int = Query(..., ge=0, le=22),
    x: int = Query(..., ge=0),
    y: int = Query(..., ge=0),
):
    """Proxy de teselas WMTS INEGI (evita bloqueo CORS en el navegador)."""
    data = _fetch_inegi_wmts_tile(z, x, y)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=7200"},
    )


@router.get("/municipios")
@router.get("/api/municipios")
@router.get("/api/municipios.php")
def municipios():
    sql = f"""
      SELECT TRIM(BOTH FROM cve_mun::text) AS cve_mun,
             TRIM(BOTH FROM nomgeo::text) AS nomgeo
        FROM {qualified(T_MUN)}
       ORDER BY CASE WHEN TRIM(cve_mun::text) ~ '^[0-9]+$'
                THEN CAST(TRIM(cve_mun::text) AS INTEGER) ELSE 99999 END, nomgeo
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            seen = set()
            rows = []
            for r in cur.fetchall():
                cve = (r.get("cve_mun") or "").strip()
                if not cve or cve in seen:
                    continue
                seen.add(cve)
                rows.append({"cve_mun": cve, "nomgeo": r.get("nomgeo") or ""})
    return {"ok": True, "count": len(rows), "rows": rows}


# --- Buscador geográfico offline (PostGIS multitabla) ---

@router.get("/buscar")
@router.get("/api/buscar")
def buscar(
    q: str = Query(..., min_length=2, max_length=120, description="Texto a buscar"),
    cve_mun: str = Query(
        "",
        description="Clave municipal (3 dígitos). Limita resultados al municipio seleccionado.",
    ),
):
    """
    Geocoder local: localidades (c_loc_punto) y colonias (c_col_ase) dentro del municipio.
    Sin ``cve_mun`` válido devuelve municipios, localidades y colonias a nivel estatal.
    """
    term = q.strip()
    cve = norm_cve_mun(cve_mun)
    scoped = bool(cve and is_mun_cve3(cve))
    rows = buscar_lugares(term, cve_mun=cve if scoped else None)
    return {
        "ok": True,
        "query": term,
        "cve_mun": cve if scoped else None,
        "count": len(rows),
        "rows": rows,
    }


@router.get("/municipio/extent")
@router.get("/api/municipio/extent")
@router.get("/api/municipio_extent.php")
def municipio_extent(cve_mun: str = Query(...)):
    """BBox WGS84 del polígono municipal (atlas.c_mun) para encuadre del mapa."""
    cve = norm_cve_mun(cve_mun)
    if not is_mun_cve3(cve):
        raise HTTPException(status_code=400, detail="INVALID_CVE")
    sql = f"""
      SELECT ST_XMin(env) AS west, ST_YMin(env) AS south,
             ST_XMax(env) AS east, ST_YMax(env) AS north
        FROM (
          SELECT ST_Envelope(ST_Transform(the_geom, 4326)) AS env
            FROM {qualified(T_MUN)}
           WHERE TRIM(BOTH FROM cve_mun::text) = %(cve)s
             AND the_geom IS NOT NULL
           LIMIT 1
        ) AS q
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"cve": cve})
            row = cur.fetchone()
    if not row or row["west"] is None:
        raise HTTPException(status_code=404, detail="MUNICIPIO_NOT_FOUND")
    west, south, east, north = float(row["west"]), float(row["south"]), float(row["east"]), float(row["north"])
    if west >= east or south >= north:
        raise HTTPException(status_code=500, detail="INVALID_BOUNDS")
    return {"ok": True, "cve_mun": cve, "bbox": {"west": west, "south": south, "east": east, "north": north}}


@router.get("/visor/colonias-labels")
@router.get("/api/visor/colonias-labels")
def colonias_labels(cve_mun: str = Query(...)):
    """Un punto de etiqueta por colonia (ST_PointOnSurface), sin duplicados por tesela MVT."""
    cve = norm_cve_mun(cve_mun)
    if not is_mun_cve3(cve):
        raise HTTPException(status_code=400, detail="INVALID_CVE")
    with get_db() as conn:
        nom_col = resolve_column(conn, SCHEMA, T_COL_ASE, ("nom_asen", "NOM_ASEN"))
        gid_col = resolve_column(conn, SCHEMA, T_COL_ASE, ("gid", "GID", "ogc_fid", "OGC_FID"))
        if not nom_col:
            raise HTTPException(status_code=500, detail="NOM_COLUMN_NOT_FOUND")
        gid_sel = f"TRIM({quote_ident(gid_col)}::text)" if gid_col else "NULL::text"
        where = mun_where_sql("", with_cvegeo=True)
        sql = f"""
          SELECT {gid_sel} AS gid,
                 TRIM({quote_ident(nom_col)}::text) AS nom_asen,
                 ST_X(ST_Transform(ST_PointOnSurface(the_geom), 4326)) AS lon,
                 ST_Y(ST_Transform(ST_PointOnSurface(the_geom), 4326)) AS lat
            FROM {qualified(T_COL_ASE)}
           WHERE the_geom IS NOT NULL
             AND TRIM(COALESCE({quote_ident(nom_col)}::text, '')) <> ''
             AND {where}
        """
        with conn.cursor() as cur:
            cur.execute(sql, {"cve": cve})
            rows = cur.fetchall()
    features = []
    for row in rows:
        lon, lat = row.get("lon"), row.get("lat")
        nom = (row.get("nom_asen") or "").strip()
        if lon is None or lat is None or not nom:
            continue
        props = {"nom_asen": nom, "NOM_ASEN": nom}
        gid = row.get("gid")
        if gid is not None and str(gid).strip():
            props["gid"] = str(gid).strip()
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                "properties": props,
            }
        )
    return {
        "ok": True,
        "cve_mun": cve,
        "count": len(features),
        "featureCollection": {"type": "FeatureCollection", "features": features},
    }


@router.get("/visor/locs-atlas-labels")
@router.get("/api/visor/locs-atlas-labels")
def locs_atlas_labels(cve_mun: str = Query(...)):
    """Un punto de etiqueta por localidad con amanzanamiento (c_l), sin duplicados por tesela MVT."""
    cve = norm_cve_mun(cve_mun)
    if not is_mun_cve3(cve):
        raise HTTPException(status_code=400, detail="INVALID_CVE")
    with get_db() as conn:
        nom_col = resolve_column(conn, SCHEMA, T_L, ("nomgeo", "NOMGEO"))
        cvegeo_col = resolve_column(conn, SCHEMA, T_L, ("cvegeo", "CVEGEO"))
        gid_col = resolve_column(conn, SCHEMA, T_L, ("gid", "GID", "ogc_fid", "OGC_FID"))
        if not nom_col and not cvegeo_col:
            raise HTTPException(status_code=500, detail="LABEL_COLUMNS_NOT_FOUND")
        gid_sel = f"TRIM({quote_ident(gid_col)}::text)" if gid_col else "NULL::text"
        nom_sel = f"TRIM({quote_ident(nom_col)}::text)" if nom_col else "NULL::text"
        cvegeo_sel = f"TRIM({quote_ident(cvegeo_col)}::text)" if cvegeo_col else "NULL::text"
        nom_pred = (
            f"TRIM(COALESCE({quote_ident(nom_col)}::text, '')) <> ''"
            if nom_col
            else "FALSE"
        )
        cvegeo_pred = (
            f"TRIM(COALESCE({quote_ident(cvegeo_col)}::text, '')) <> ''"
            if cvegeo_col
            else "FALSE"
        )
        where = mun_where_sql("", with_cvegeo=True)
        sql = f"""
          SELECT {gid_sel} AS gid,
                 {nom_sel} AS nomgeo,
                 {cvegeo_sel} AS cvegeo,
                 ST_X(ST_Transform(ST_PointOnSurface(the_geom), 4326)) AS lon,
                 ST_Y(ST_Transform(ST_PointOnSurface(the_geom), 4326)) AS lat
            FROM {qualified(T_L)}
           WHERE the_geom IS NOT NULL
             AND ({nom_pred} OR {cvegeo_pred})
             AND {where}
        """
        with conn.cursor() as cur:
            cur.execute(sql, {"cve": cve})
            rows = cur.fetchall()
    features = []
    for row in rows:
        lon, lat = row.get("lon"), row.get("lat")
        nom = (row.get("nomgeo") or "").strip()
        cvegeo = (row.get("cvegeo") or "").strip()
        if lon is None or lat is None or (not nom and not cvegeo):
            continue
        props = {}
        if nom:
            props["nomgeo"] = nom
            props["NOMGEO"] = nom
        if cvegeo:
            props["cvegeo"] = cvegeo
            props["CVEGEO"] = cvegeo
        gid = row.get("gid")
        if gid is not None and str(gid).strip():
            props["gid"] = str(gid).strip()
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                "properties": props,
            }
        )
    return {
        "ok": True,
        "cve_mun": cve,
        "count": len(features),
        "featureCollection": {"type": "FeatureCollection", "features": features},
    }


_geo_contexto_bulk_cache: Optional[Dict[str, Dict[str, Any]]] = None


def _load_geo_contexto_bulk() -> Dict[str, Dict[str, Any]]:
    global _geo_contexto_bulk_cache
    if _geo_contexto_bulk_cache is not None:
        return _geo_contexto_bulk_cache
    sql = f"""
      SELECT TRIM(ent::text) AS ent, TRIM(cve_mun::text) AS cve_mun,
             ubicacion, superficie, relieve, clima, hidrografia, uso_suelo
        FROM {qualified(T_CONTEXTO)}
       WHERE TRIM(ent::text) = '12'
    """
    rows: Dict[str, Dict[str, Any]] = {}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for row in cur.fetchall():
                cve = norm_cve_mun(row.get("cve_mun"))
                if cve:
                    rows[cve] = dict(row)
    _geo_contexto_bulk_cache = rows
    return rows


@router.get("/geo/contexto")
@router.get("/api/geo/contexto")
@router.get("/api/geo_contexto.php")
def geo_contexto(cve_mun: str = Query(...)):
    cve = norm_cve_mun(cve_mun)
    rows = _load_geo_contexto_bulk()
    row = rows.get(cve)
    return {"ok": True, "row": row}


@router.get("/geo/contexto/all")
@router.get("/api/geo/contexto/all")
@router.get("/api/geo_contexto_all.php")
def geo_contexto_all():
    rows = _load_geo_contexto_bulk()
    return {"ok": True, "rows": rows}


@router.get("/municipio/at-click")
@router.get("/api/municipio/at-click")
@router.get("/api/municipio_at_click.php")
def municipio_at_click(lon: float = Query(...), lat: float = Query(...)):
    sql = f"""
      WITH pt AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) AS g),
      cand AS (
        SELECT TRIM(m.cve_mun::text) AS cve_mun, TRIM(m.nomgeo::text) AS nomgeo,
               ST_MakeValid(ST_Transform(m.the_geom, 4326)) AS geom4326
          FROM {qualified(T_MUN)} m CROSS JOIN pt
         WHERE m.the_geom IS NOT NULL
           AND ST_Transform(m.the_geom, 4326) && ST_Expand(pt.g, 0.0008)
      ),
      ranked AS (
        SELECT cve_mun, nomgeo,
               CASE WHEN ST_Contains(geom4326, pt.g) THEN 0 ELSE 1 END AS prio,
               ST_Distance(geom4326::geography, pt.g::geography) AS dist_m
          FROM cand CROSS JOIN pt
         WHERE ST_Contains(geom4326, pt.g)
            OR ST_DWithin(geom4326::geography, pt.g::geography, 40)
      )
      SELECT cve_mun, nomgeo FROM ranked ORDER BY prio, dist_m LIMIT 1
    """
    with get_db() as conn:
        conn.execute("SET statement_timeout TO 8000")
        with conn.cursor() as cur:
            cur.execute(sql, {"lon": lon, "lat": lat})
            row = cur.fetchone()
    if not row:
        return {"ok": True, "hit": None}
    return {"ok": True, "hit": {"cve_mun": norm_cve_mun(row["cve_mun"]), "nomgeo": row["nomgeo"] or ""}}


@router.get("/explorador/municipal")
@router.get("/api/explorador/municipal")
@router.get("/api/explorador_municipal.php")
def explorador_municipal(cve_mun: Optional[str] = None):
    cve, _ = _sel_params(cve_mun, None)
    try:
        with get_db() as conn:
            return build_explorador_response(conn, cve)
    except Exception as e:
        raise HTTPException(500, detail={"ok": False, "message": str(e)}) from e


@router.get("/explorador/municipal/all")
@router.get("/api/explorador/municipal/all")
@router.get("/api/explorador_municipal_all.php")
def explorador_municipal_all():
    try:
        with get_db() as conn:
            return build_explorador_all_response(conn)
    except Exception as e:
        raise HTTPException(500, detail={"ok": False, "message": str(e)}) from e


# --- Comparativas ---

def _comparativa_tab(sort_key: str, extra, fmt_row, cve, nom):
    with get_db() as conn:
        rows = load_tab_municipal_rows(conn, extra)
    if not rows:
        raise HTTPException(500, detail={"ok": False, "error": "NO_DATA"})
    return build_top_bottom_response(rows, sort_key, cve, nom, fmt_row)


@router.get("/comparativas/poblacion")
@router.get("/api/comparativas/poblacion")
@router.get("/api/poblacion_comparativa.php")
def poblacion_comparativa(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)

    def fmt(r, h):
        return {
            "cve_mun": r["cve_mun"], "nom_mun": r["nom_mun"],
            "pob_tot": r["pob_tot"], "pob_tot_2010": r["pob_tot_2010"], "highlight": h,
        }

    return _comparativa_tab(
        "pob_tot",
        [("pob_tot", ("pop_tot", "POP_TOT", "pob_tot"), ""), ("pob_tot_2010", ("pob_tot_2010", "POB_TOT_2010"), "")],
        fmt, cve, nom,
    )


@router.get("/comparativas/crecimiento")
@router.get("/api/comparativas/crecimiento")
@router.get("/api/crecimiento_comparativa.php")
def crecimiento_comparativa(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)

    def fmt(r, h):
        return {
            "cve_mun": r["cve_mun"], "nom_mun": r["nom_mun"],
            "dist_porc": r["dist_porc"], "creci_00_10": r["creci_00_10"],
            "creci_10_20": r["creci_10_20"], "highlight": h,
        }

    return _comparativa_tab(
        "dist_porc",
        [
            ("dist_porc", ("dist_porc", "DIST_PORC"), ""),
            ("creci_00_10", ("creci_00_10", "CRECI_00_10", "pcreci_00_10", "PCRECI_00_10"), ""),
            ("creci_10_20", ("creci_10_20", "CRECI_10_20", "pcreci_10_20", "PCRECI_10_20"), ""),
        ],
        fmt, cve, nom,
    )


@router.get("/comparativas/edad-mediana")
@router.get("/api/comparativas/edad-mediana")
@router.get("/api/edad_mediana_comparativa.php")
def edad_mediana(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)

    def fmt(r, h):
        return {"cve_mun": r["cve_mun"], "nom_mun": r["nom_mun"], "edad_mediana": r["edad_mediana"], "highlight": h}

    return _comparativa_tab("edad_mediana", [("edad_mediana", ("edad_mediana", "EDAD_MEDIANA"), "")], fmt, cve, nom)


@router.get("/comparativas/superficie")
@router.get("/api/comparativas/superficie")
@router.get("/api/superficie_comparativa.php")
def superficie_comparativa(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    col_cve = col_nom = col_porc = None
    with get_db() as conn:
        col_cve = resolve_column(conn, SCHEMA, T_MUN, ("cve_mun",))
        col_nom = resolve_column(conn, SCHEMA, T_MUN, ("nomgeo", "nom_mun"))
        col_porc = resolve_column(conn, SCHEMA, T_MUN, ("porcsup",))
        if not all([col_cve, col_nom, col_porc]):
            raise HTTPException(500, detail={"ok": False, "error": "COLUMNS_NOT_FOUND"})
        sql = f"""
          SELECT TRIM({quote_ident(col_cve)}::text) AS cve_mun,
                 TRIM({quote_ident(col_nom)}::text) AS nom_mun,
                 {quote_ident(col_porc)} AS porcsup
            FROM {qualified(T_MUN)}
        """
        with conn.cursor() as cur:
            cur.execute(sql)
            db = cur.fetchall()
    rows = []
    for r in db:
        if not is_mun_cve3(r.get("cve_mun")):
            continue
        nom = (r.get("nom_mun") or "").strip()
        if not nom:
            continue
        rows.append({
            "cve_mun": norm_cve_mun(r["cve_mun"]),
            "nom_mun": nom,
            "porcsup": row_numeric(r, ("porcsup",), 0),
        })
    if not rows:
        raise HTTPException(500, detail={"ok": False, "error": "NO_DATA"})

    def fmt(r, h):
        return {"cve_mun": r["cve_mun"], "nom_mun": r["nom_mun"], "porcsup": r["porcsup"], "highlight": h}

    return build_top_bottom_response(rows, "porcsup", cve, nom, fmt)


# --- Vistas tab_municipal ---

@router.get("/vistas/vivienda-servicios")
@router.get("/api/vistas/vivienda-servicios")
@router.get("/api/vivienda_servicios_vista.php")
def vivienda_servicios(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    keys = ("por_redo_ener", "por_redo_agua", "por_redo_drenaje")
    with get_db() as conn:
        data = fetch_nacional_estatal_municipio(conn, cve, nom, keys)
    return {"ok": True, **data, "cve_mun_selected": cve or None}


@router.get("/vistas/vivienda-participacion")
@router.get("/api/vistas/vivienda-participacion")
@router.get("/api/vivienda_participacion_vista.php")
def vivienda_participacion(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    try:
        with get_db() as conn:
            return build_vivienda_participacion_response(conn, cve, nom)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/vistas/unidades-economicas")
@router.get("/api/vistas/unidades-economicas")
@router.get("/api/unidades_economicas_vista.php")
def unidades_economicas(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)

    def fmt(r, h):
        return {"cve_mun": r["cve_mun"], "nom_mun": r["nom_mun"], "ue_den": r["ue_den"], "highlight": h}

    return _comparativa_tab("ue_den", [("ue_den", ("ue_den", "UE_DEN"), "")], fmt, cve, nom)


@router.get("/vistas/unidades-medicas")
@router.get("/api/vistas/unidades-medicas")
@router.get("/api/unidades_medicas_vista.php")
def unidades_medicas(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    try:
        with get_db() as conn:
            return build_unidades_medicas_response(conn, cve, nom)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/vistas/nacimientos")
@router.get("/api/vistas/nacimientos")
@router.get("/api/nacimientos_vista.php")
def nacimientos_vista(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    return _nac_def_vista(cve_mun, nom_mun, kind="nac")


@router.get("/vistas/defunciones")
@router.get("/api/vistas/defunciones")
@router.get("/api/defunciones_vista.php")
def defunciones_vista(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    return _nac_def_vista(cve_mun, nom_mun, kind="def")


def _row_opt(row, keys):
    """Como row_numeric pero devuelve None si no hay dato (PHP nac_row_numeric)."""
    for k in keys:
        if k not in row or row[k] is None or row[k] == "":
            continue
        try:
            return float(row[k])
        except (TypeError, ValueError):
            continue
    return None


def _nac_def_vista(cve_mun, nom_mun, kind: str):
    cve, nom = _sel_params(cve_mun, nom_mun)
    if kind == "nac":
        nat_key = "naci_24"
        mun_col = "por_naci_2024_redo"
        por_ent_alias = "por_naci24"
        nat_col_cands = ("naci_24", "NACI_24", "naci24", "NACI24")
        por_ent_col_cands = ("por_naci24", "POR_NACI24", "por_naci_24", "POR_NACI_24")
        mun_col_cands = (
            "porc_naci_2024_redo", "PORC_NACI_2024_REDO",
            "por_naci_2024_redo", "POR_NACI_2024_REDO",
            "por_naci_2024", "POR_NACI_2024",
        )
    else:
        nat_key = "defu"
        mun_col = "por_def_2024_redo"
        por_ent_alias = "por_def_ent"
        nat_col_cands = ("defu", "DEFU", "def_u", "DEF_U")
        por_ent_col_cands = (
            "defu_por", "DEFU_POR", "por_def_24", "POR_DEF_24",
            "por_def24", "POR_DEF24", "por_defu_24", "POR_DEFU_24",
            "por_def_2024", "POR_DEF_2024",
        )
        mun_col_cands = (
            "por_def_2024_redo", "POR_DEF_2024_REDO",
            "porc_def_2024_redo", "PORC_DEF_2024_REDO",
            "por_def_2024", "POR_DEF_2024",
        )

    try:
        with get_db() as conn:
            col_ent = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("ent", "ENT", "cve_ent", "CVE_ENT"))
            col_nom = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("nom_ent", "NOM_ENT", "nomgeo", "NOMGEO"))
            col_nat = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, nat_col_cands)
            col_por_ent = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, por_ent_col_cands)
            col_est = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("estatal", "ESTATAL"))
            col_mun_por = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, mun_col_cands)

            missing = []
            if not col_ent:
                missing.append("ent")
            if not col_nom:
                missing.append("nom_ent")
            if not col_nat:
                missing.append(nat_key)
            if not col_por_ent:
                missing.append(por_ent_alias)
            if not col_mun_por:
                missing.append(mun_col)
            if missing:
                raise HTTPException(
                    500,
                    detail={
                        "ok": False,
                        "error": "COLUMNS_NOT_FOUND",
                        "message": "No se encontraron columnas: " + ", ".join(missing),
                    },
                )

            est_sql = (
                f", TRIM(t.{quote_ident(col_est)}::text) AS estatal"
                if col_est
                else ", ''::text AS estatal"
            )
            sql_nat = f"""
              SELECT TRIM(t.{quote_ident(col_ent)}::text) AS ent,
                     TRIM(t.{quote_ident(col_nom)}::text) AS nom_ent,
                     t.{quote_ident(col_nat)} AS {nat_key},
                     t.{quote_ident(col_por_ent)} AS {por_ent_alias}
                     {est_sql}
                FROM {qualified(T_TAB_NACIONAL)} t
            """
            with conn.cursor() as cur:
                cur.execute(sql_nat)
                nat_rows = cur.fetchall()
            rows = load_tab_municipal_rows(conn, [(mun_col, mun_col_cands, "")])
    except ValueError as exc:
        raise HTTPException(
            500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc

    states = []
    por_guerrero = None
    for r in nat_rows:
        ek = ent_key_to_int(r.get("ent"))
        if ek < 1:
            continue
        nm = (r.get("nom_ent") or "").strip()
        if not nm:
            continue
        metric = _row_opt(r, (nat_key,))
        if metric is None:
            continue
        est_raw = (r.get("estatal") or "").strip().lower()
        estatal_si = est_raw == "si" if col_est else ek == 12
        states.append({
            "ent": str(ek).zfill(2),
            "nom_ent": nm,
            nat_key: metric,
            "estatal_si": estatal_si,
        })
        if estatal_si and por_guerrero is None:
            por_guerrero = _row_opt(r, (por_ent_alias,))

    states.sort(key=lambda s: (-s.get(nat_key, 0), s.get("nom_ent", "")))

    if not states:
        raise HTTPException(
            500,
            detail={
                "ok": False,
                "error": "NO_DATA",
                "message": "No hay filas válidas (ent 01–32) en atlas.tab_nacional.",
            },
        )
    if not rows:
        raise HTTPException(
            500,
            detail={
                "ok": False,
                "error": "NO_DATA",
                "message": "No hay filas en atlas.tab_municipal con cve_mun de 3 dígitos.",
            },
        )

    def fmt(r, h):
        return {
            "cve_mun": r["cve_mun"],
            "nom_mun": r["nom_mun"],
            mun_col: r.get(mun_col),
            "highlight": h,
        }

    out = build_top_bottom_response(rows, mun_col, cve, nom, fmt)
    out["states"] = states
    out["por_entidad_guerrero"] = por_guerrero
    return out


# --- Más vistas (ranking genérico) ---

def _vista_simple(path_key: str, sort_col, col_candidates, fields_out):
    """Factory interno — no usado en runtime; endpoints explícitos abajo."""

    pass


@router.get("/vistas/escolaridad")
@router.get("/api/vistas/escolaridad")
@router.get("/api/escolaridad_vista.php")
def escolaridad_vista(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    try:
        with get_db() as conn:
            return build_escolaridad_response(conn, cve, nom)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/vistas/analfabetismo")
@router.get("/api/vistas/analfabetismo")
@router.get("/api/analfabetismo_vista.php")
def analfabetismo_vista(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    try:
        with get_db() as conn:
            return build_analfabetismo_response(conn, cve, nom)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/vistas/poblacion-ocupada")
@router.get("/api/vistas/poblacion-ocupada")
@router.get("/api/poblacion_ocupada_vista.php")
def poblacion_ocupada(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    try:
        with get_db() as conn:
            return build_poblacion_ocupada_response(conn, cve, nom)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/vistas/caracteristicas-economicas")
@router.get("/api/vistas/caracteristicas-economicas")
@router.get("/api/caracteristicas_economicas_vista.php")
def caracteristicas_economicas(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    try:
        with get_db() as conn:
            return build_caracteristicas_economicas_response(conn, cve, nom)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/vistas/superficie-agricultura")
@router.get("/api/vistas/superficie-agricultura")
@router.get("/api/superficie_agricultura_vista.php")
def superficie_agricultura(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    try:
        with get_db() as conn:
            return build_superficie_agricultura_response(conn, cve, nom)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/vistas/inversion-publica")
@router.get("/api/vistas/inversion-publica")
@router.get("/api/inversion_publica_vista.php")
def inversion_publica(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    try:
        with get_db() as conn:
            return build_inversion_publica_response(conn, cve, nom)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/vistas/instituciones-admin-publica")
@router.get("/api/vistas/instituciones-admin-publica")
@router.get("/api/instituciones_admin_publica_municipal_vista.php")
def instituciones_admin(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    try:
        with get_db() as conn:
            return build_instituciones_admin_response(conn, cve, nom)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/vistas/habitantes-por-policia")
@router.get("/api/vistas/habitantes-por-policia")
@router.get("/api/habitantes_por_policia_vista.php")
def habitantes_policia(cve_mun: Optional[str] = None, nom_mun: Optional[str] = None):
    cve, nom = _sel_params(cve_mun, nom_mun)
    with get_db() as conn:
        rows = load_tab_municipal_rows(
            conn,
            [
                ("habxpol", ("habxpol",), ""),
                ("pob_tot", ("pob_tot", "pop_tot"), ""),
                ("pol_prev", ("pol_prev",), ""),
            ],
        )
    for r in rows:
        hp = row_numeric(r, ("habxpol",), None)
        if hp is None:
            pol = row_numeric(r, ("pol_prev",), 0)
            pob = row_numeric(r, ("pob_tot",), 0)
            r["habxpol_eff"] = (pob / pol) if pol > 0 else 0
        else:
            r["habxpol_eff"] = hp

    def fmt(r, h):
        return {
            "cve_mun": r["cve_mun"], "nom_mun": r["nom_mun"],
            "habxpol": r.get("habxpol_eff", r.get("habxpol")),
            "pob_tot": r["pob_tot"], "pol_prev": r["pol_prev"], "highlight": h,
        }

    return build_top_bottom_response(rows, "habxpol_eff", cve, nom, fmt)


# --- Indicadores / columnas ---

@router.get("/poblacion/columns")
@router.get("/api/poblacion/columns")
@router.get("/api/poblacion_columns.php")
def poblacion_columns():
    sql = """
      SELECT table_name, column_name, data_type
        FROM information_schema.columns
       WHERE table_schema = %s
         AND table_name IN (%s, %s, %s)
       ORDER BY table_name, ordinal_position
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (SCHEMA, T_TAB_MUNICIPAL, T_TAB_NACIONAL, T_MUN))
            rows = cur.fetchall()
    return {"ok": True, "columns": rows}


@router.get("/indicators")
@router.get("/api/indicators")
@router.get("/api/indicator.php")
def indicator(indicatorId: str = Query(...)):
    queries = {
        "geo_altitud": {
            "sql": """
              SELECT m.nom_mun AS municipio, COALESCE(i.valor, 0) AS valor
                FROM public.c_mun_attr m
                LEFT JOIN public.indicadores_mock i
                  ON i.clave_mun = m.cvegeo AND i.indicator_id = %(indicatorId)s
               WHERE m.cve_ent = '12'
               ORDER BY valor DESC LIMIT 200
            """,
        }
    }
    if indicatorId not in queries:
        raise HTTPException(404, detail={"ok": False, "error": "INDICATOR_NOT_MAPPED"})
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(queries[indicatorId]["sql"], {"indicatorId": indicatorId})
            rows = [
                {"municipio": r.get("municipio", ""), "valor": r.get("valor")}
                for r in cur.fetchall()
            ]
    return {"ok": True, "indicatorId": indicatorId, "rows": rows}


# --- Inventario INV bbox ---

@router.get("/inv/bbox")
@router.get("/api/inv/bbox")
@router.get("/api/inv_bbox.php")
def inv_bbox(
    cve_mun: str = Query(...),
    field: str = Query(...),
    xmin: float = Query(...),
    ymin: float = Query(...),
    xmax: float = Query(...),
    ymax: float = Query(...),
    mode: str = Query("point"),
):
    from utils import mun_where_sql as mws

    allowed_point = {
        "pobtot", "pobfem", "pobmas", "pob0_14", "p15a29a", "p30a59a", "p_60ymas",
        "p_cd_t", "graproes", "graproes_f", "graproes_m", "vivtot", "vivpar",
        "tvipahab", "vivnohab",
    }
    allowed_poly = {"alumpub_c", "recucall_c"}
    mode = (mode or "point").lower()
    field = field.lower()
    allowed = allowed_poly if mode == "polygon" else allowed_point
    if field not in allowed:
        raise HTTPException(400, detail={"ok": False, "error": "FIELD_NOT_ALLOWED"})
    cve = norm_cve_mun(cve_mun)
    qf = quote_ident(field)
    where = mws("")
    features = []
    with get_db() as conn:
        with conn.cursor() as cur:
            if mode == "polygon":
                sql = f"""
                  SELECT ST_AsGeoJSON(ST_Transform(the_geom, 4326), 6) AS geom_json,
                         cvegeo, cve_mza, ambito, pobtot, pobfem, pobmas,
                         {qf} AS val
                    FROM atlas.c_inv
                   WHERE the_geom IS NOT NULL
                     AND the_geom && ST_MakeEnvelope(%(xmin)s, %(ymin)s, %(xmax)s, %(ymax)s, 900913)
                     AND {where}
                   LIMIT 8000
                """
            else:
                sql = f"""
                  SELECT ST_X(ST_Transform(ST_PointOnSurface(the_geom), 4326)) AS lon,
                         ST_Y(ST_Transform(ST_PointOnSurface(the_geom), 4326)) AS lat,
                         cvegeo, cve_mza, ambito, pobtot, pobfem, pobmas,
                         {qf} AS val
                    FROM atlas.c_inv
                   WHERE the_geom IS NOT NULL
                     AND the_geom && ST_MakeEnvelope(%(xmin)s, %(ymin)s, %(xmax)s, %(ymax)s, 900913)
                     AND {where}
                   LIMIT 12000
                """
            cur.execute(
                sql,
                {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax, "cve": cve},
            )
            import json as _json
            for row in cur.fetchall():
                props = {
                    "cvegeo": (row.get("cvegeo") or "").strip(),
                    "cve_mza": (row.get("cve_mza") or "").strip(),
                    "ambito": (row.get("ambito") or "").strip(),
                    "pobtot": row.get("pobtot"),
                    "pobfem": row.get("pobfem"),
                    "pobmas": row.get("pobmas"),
                    "value": row.get("val"),
                }
                if mode == "polygon":
                    gj = (row.get("geom_json") or "").strip()
                    if not gj:
                        continue
                    geom = _json.loads(gj)
                    features.append({"type": "Feature", "geometry": geom, "properties": props})
                else:
                    lon, lat = row.get("lon"), row.get("lat")
                    if lon is None or lat is None:
                        continue
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                        "properties": props,
                    })
    return {
        "ok": True,
        "mode": mode,
        "field": field,
        "cve_mun": cve,
        "count": len(features),
        "featureCollection": {"type": "FeatureCollection", "features": features},
    }


# --- Análisis espacial dinámico (polígono + campos INV) ---


class AnalisisDinamicoBody(BaseModel):
    """Cuerpo POST /api/analisis/dinamico: polígono GeoJSON y columnas a agregar."""

    tabla: str = Field(..., description="Identificador de capa (ej. c_inv)")
    campos_elegidos: List[str] = Field(..., min_length=1)
    geojson: Dict[str, Any] = Field(..., description="Feature, Geometry o FeatureCollection")
    cve_mun: Optional[str] = Field(None, description="Filtro municipal opcional (3 dígitos)")


@router.get("/analisis/capas")
@router.get("/api/analisis/capas")
def analisis_listar_capas():
    """Catálogo de capas disponibles para análisis espacial (paso 1 del flujo UI)."""
    return {"ok": True, "capas": listar_capas_disponibles()}


@router.get("/capas/{nombre_tabla}/columnas")
@router.get("/api/capas/{nombre_tabla}/columnas")
def capas_columnas(nombre_tabla: str):
    """
    Descubre columnas numéricas agregables vía information_schema (paso 2).

    No requiere modelos Pydantic por columna: la lista se genera en caliente desde Postgres.
    """
    try:
        with get_db() as conn:
            columnas = listar_columnas_numericas(conn, nombre_tabla)
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(
            400,
            detail={"ok": False, "error": code, "message": "Tabla no habilitada para análisis."},
        ) from exc
    return {"ok": True, "tabla": nombre_tabla.lower(), "columnas": columnas, "total": len(columnas)}


@router.post("/analisis/dinamico")
@router.post("/api/analisis/dinamico")
def analisis_dinamico(body: AnalisisDinamicoBody):
    """
    Ejecuta SUM/AVG espacial sobre campos elegidos intersectando el polígono (paso 4).

    PostGIS resuelve ST_Intersects + agregaciones en una sola consulta.
    """
    try:
        with get_db() as conn:
            resultado = ejecutar_analisis_espacial(
                conn,
                nombre_tabla=body.tabla,
                campos_elegidos=body.campos_elegidos,
                geojson=body.geojson,
                cve_mun=body.cve_mun,
            )
        return resultado
    except ValueError as exc:
        msg = str(exc)
        err = msg.split(":", 1)[0]
        raise HTTPException(
            400,
            detail={"ok": False, "error": err, "message": msg},
        ) from exc
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "QUERY_FAILED",
                "message": str(exc),
            },
        )


# --- Visor buffer (selección en mapa) ---


class VisorBufferBody(BaseModel):
    """POST /api/visor/buffer: buffer geodésico sobre un feature GeoJSON."""

    geojson: Dict[str, Any] = Field(..., description="Feature o Geometry seleccionado en el mapa")
    distance_m: float = Field(..., gt=0, le=500_000, description="Radio del buffer en metros")
    layer_id: Optional[str] = Field(
        None,
        description="Id de capa del catálogo visor (p. ej. hidro_cuerpos); usa geometría PostGIS por gid",
    )
    source_gid: Optional[str] = Field(None, description="gid del elemento en la capa temática")
    line_side: Optional[str] = Field(
        None,
        description="Inundación lateral en líneas: left, right o both (corredor)",
    )


_BUFFER_ERRORS = {
    "GEOMETRIA_INVALIDA": "Geometría no válida para buffer.",
    "DISTANCIA_INVALIDA": "Indica una distancia válida en metros (mayor que 0).",
    "BUFFER_FALLIDO": "PostGIS no pudo generar el área de influencia.",
    "CAPA_INVALIDA": "Capa no reconocida para buffer.",
    "GID_NO_DISPONIBLE": "El elemento no tiene identificador en la base de datos.",
    "ELEMENTO_NO_ENCONTRADO": "No se encontró el elemento en PostGIS.",
    "POSTGIS_ERROR": "PostGIS no pudo procesar la geometría.",
    "LADO_INVALIDO": "Lado de inundación no válido (left, right o both).",
}


def _visor_buffer_http_error(exc: Exception) -> HTTPException:
    import logging

    logging.getLogger(__name__).exception("visor buffer failed: %s", exc)
    if isinstance(exc, ValueError):
        code = str(exc)
        return HTTPException(
            400,
            detail={
                "ok": False,
                "error": code,
                "message": _BUFFER_ERRORS.get(code, code),
            },
        )
    try:
        import psycopg
    except ImportError:
        psycopg = None  # type: ignore
    if psycopg is not None and isinstance(exc, psycopg.Error):
        return HTTPException(
            400,
            detail={
                "ok": False,
                "error": "POSTGIS_ERROR",
                "message": _BUFFER_ERRORS["POSTGIS_ERROR"],
            },
        )
    return HTTPException(
        500,
        detail={
            "ok": False,
            "error": "BUFFER_INTERNO",
            "message": "Error interno al generar el área de influencia.",
        },
    )


@router.post("/visor/buffer")
@router.post("/api/visor/buffer")
def visor_buffer(body: VisorBufferBody):
    try:
        with get_db() as conn:
            feature = buffer_geometry_geojson(
                conn,
                body.geojson,
                body.distance_m,
                layer_id=body.layer_id,
                source_gid=body.source_gid,
                line_side=body.line_side,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise _visor_buffer_http_error(exc) from exc
    return {"ok": True, "feature": feature, "distance_m": body.distance_m}


@router.get("/visor/feature-geometry")
@router.get("/api/visor/feature-geometry")
def visor_feature_geometry(
    layer_id: str = Query(..., min_length=1),
    gid: str = Query(..., min_length=1),
):
    """Geometría completa de un elemento (resaltado en mapa sin depender de tiles)."""
    try:
        with get_db() as conn:
            feature = fetch_feature_geometry_geojson(conn, layer_id.strip().lower(), gid.strip())
    except HTTPException:
        raise
    except Exception as exc:
        raise _visor_buffer_http_error(exc) from exc
    return {"ok": True, "feature": feature}


@router.get("/visor/feature-outline")
@router.get("/api/visor/feature-outline")
def visor_feature_outline(
    layer_id: str = Query(..., min_length=1),
    gid: str = Query(..., min_length=1),
):
    """Contorno simplificado de un polígono (resaltado sin artefactos de tiles)."""
    try:
        with get_db() as conn:
            feature = fetch_feature_outline_geojson(conn, layer_id.strip().lower(), gid.strip())
    except HTTPException:
        raise
    except Exception as exc:
        raise _visor_buffer_http_error(exc) from exc
    return {"ok": True, "feature": feature}


# --- Visor export ---

@router.get("/visor/layers")
@router.get("/api/visor/layers")
def visor_layers_list():
    cat = layer_catalog()
    return {"ok": True, "layers": [{"id": k, **v} for k, v in cat.items()]}


def _visor_tabular_http_error(exc: Exception) -> HTTPException:
    raw = str(exc)
    code = raw.split(":", 1)[0]
    if code in ("UNKNOWN_LAYER", "MISSING_CVE_MUN", "NO_ROWS", "NO_COLUMNS"):
        status = 404 if code == "NO_ROWS" else 400
        return HTTPException(
            status_code=status,
            detail={"ok": False, "error": code, "message": tabular_error_message(code)},
        )
    if code == "EXPORT_FAILED":
        return HTTPException(
            status_code=500,
            detail={"ok": False, "error": code, "message": tabular_error_message(code)},
        )
    return HTTPException(status_code=500, detail={"ok": False, "message": raw})


@router.get("/visor/tabla/capas")
@router.get("/api/visor/tabla/capas")
def visor_tabular_layers_list():
    """Catálogo de capas con consulta tabular en el visor."""
    return {"ok": True, "layers": list_tabular_layers()}


@router.get("/visor/tabla")
@router.get("/api/visor/tabla")
def visor_tabular_query(
    layer: str = Query(..., min_length=1),
    cve_mun: str = Query(..., min_length=1),
):
    """Datos tabulares de una capa del visor filtrados por municipio."""
    try:
        with get_db() as conn:
            data = fetch_tabular_data(conn, layer.strip().lower(), cve_mun.strip())
    except HTTPException:
        raise
    except ValueError as exc:
        raise _visor_tabular_http_error(exc) from exc
    except Exception as exc:
        raise _visor_tabular_http_error(exc) from exc
    return {"ok": True, **data}


@router.get("/visor/tabla/export")
@router.get("/api/visor/tabla/export")
def visor_tabular_export(
    layer: str = Query(..., min_length=1),
    cve_mun: str = Query(..., min_length=1),
    format: str = Query("xlsx", alias="format"),
):
    """Exporta la consulta tabular a Excel (.xlsx)."""
    fmt = (format or "xlsx").strip().lower()
    if fmt not in ("xlsx", "excel"):
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "INVALID_FORMAT",
                "message": "format debe ser xlsx",
            },
        )
    try:
        with get_db() as conn:
            data = fetch_tabular_data(conn, layer.strip().lower(), cve_mun.strip())
            xlsx = build_tabular_xlsx(data)
    except HTTPException:
        raise
    except ValueError as exc:
        raise _visor_tabular_http_error(exc) from exc
    except Exception as exc:
        raise _visor_tabular_http_error(exc) from exc

    cve = norm_cve_mun(cve_mun) or "mun"
    nom = (data.get("nom_mun") or "municipio").replace(" ", "_")
    filename = f"localidades_{nom}_{cve}.xlsx"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/visor/export")
@router.get("/api/visor/export")
@router.get("/api/visor_export.php")
def visor_export(
    layer: str = Query(...),
    format: str = Query(..., alias="format"),
    cve_mun: str = Query(...),
    nom_mun: Optional[str] = None,
):
    fmt = format.lower()
    if fmt not in ("kml", "shp"):
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "INVALID_FORMAT",
                "message": export_error_message("INVALID_FORMAT"),
            },
        )
    try:
        with get_db() as conn:
            data, filename, mime = export_layer(conn, layer, fmt, cve_mun, nom_mun or "")
        return Response(
            content=data,
            media_type=mime,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )
    except ValueError as exc:
        raw = str(exc)
        code = raw.split(":", 1)[0]
        status = 404 if code in ("NO_FEATURES", "NO_GEOMETRIES") else 400
        if code.startswith("SHP_WRITE_FAILED"):
            status = 500
            code = "SHP_WRITE_FAILED"
        return JSONResponse(
            status_code=status,
            content={
                "ok": False,
                "error": code,
                "message": export_error_message(code),
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "QUERY_FAILED",
                "message": str(exc),
            },
        )
