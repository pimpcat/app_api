"""Datasource espacial: PostGIS → geometría (Atlas o GroSIG_Cartography)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from shapely import wkb as shapely_wkb
from shapely.geometry import GeometryCollection, LineString, MultiLineString, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from cartography_engine.config import get_cartography_settings
from cartography_engine.layers import (
    ALLOWED_LAYER_TABLES,
    LayerData,
    LayerDef,
    is_cartography_table,
    parse_table_ref,
)
from cartography_engine.models import CartographyError

log = logging.getLogger("cartography_engine.datasource")


@dataclass(frozen=True)
class MunicipalityFeature:
    cve_mun: str
    nomgeo: str
    geometry: BaseGeometry
    crs: str
    cve_ent: str = "12"
    ent_nomgeo: str = "Guerrero"


@dataclass(frozen=True)
class LocalityFeature:
    cve_ent: str
    cve_mun: str
    cve_loc: str
    nomgeo: str
    ambito: str
    geometry: BaseGeometry
    crs: str
    mun_nomgeo: str = ""
    ent_nomgeo: str = ""


@dataclass(frozen=True)
class MunicipalityRef:
    cve_mun: str
    nomgeo: str


def _norm_cve3(cve_mun: str) -> str:
    digits = "".join(ch for ch in str(cve_mun or "") if ch.isdigit())
    if not digits:
        return ""
    return digits[-3:].zfill(3) if len(digits) >= 3 else digits.zfill(3)


def _norm_cve2(cve_ent: str) -> str:
    digits = "".join(ch for ch in str(cve_ent or "") if ch.isdigit())
    if not digits:
        return "12"
    return digits[-2:].zfill(2)


def _norm_cve4(cve_loc: str) -> str:
    digits = "".join(ch for ch in str(cve_loc or "") if ch.isdigit())
    if not digits:
        return ""
    return digits[-4:].zfill(4) if len(digits) >= 4 else digits.zfill(4)


def _parse_geojson(geojson_raw: Any) -> Optional[BaseGeometry]:
    if not geojson_raw:
        return None
    if isinstance(geojson_raw, (bytes, bytearray)):
        geojson_raw = geojson_raw.decode("utf-8")
    if isinstance(geojson_raw, str):
        geom_dict: dict[str, Any] = json.loads(geojson_raw)
    else:
        geom_dict = geojson_raw
    geom = shape(geom_dict)
    if geom.is_empty:
        return None
    return geom


def _parse_wkb(wkb_raw: Any) -> Optional[BaseGeometry]:
    """WKB/EWKB desde PostGIS (ST_AsBinary). Más barato que GeoJSON."""
    if not wkb_raw:
        return None
    try:
        if isinstance(wkb_raw, memoryview):
            wkb_raw = wkb_raw.tobytes()
        elif isinstance(wkb_raw, bytearray):
            wkb_raw = bytes(wkb_raw)
        elif isinstance(wkb_raw, str):
            # hex EWKB
            wkb_raw = bytes.fromhex(wkb_raw)
        geom = shapely_wkb.loads(wkb_raw)
    except Exception:
        try:
            from shapely import from_wkb

            geom = from_wkb(wkb_raw)
        except Exception:
            return None
    if geom is None or geom.is_empty:
        return None
    return geom


def _parse_row_geom(row: dict[str, Any]) -> Optional[BaseGeometry]:
    """Prefiere WKB; fallback GeoJSON (etiquetas / paths legacy)."""
    if "wkb" in row and row.get("wkb") is not None:
        g = _parse_wkb(row.get("wkb"))
        if g is not None:
            return g
    return _parse_geojson(row.get("geojson"))


def _localidades_a_ambito_want(layer: Any) -> Optional[str]:
    """Si la capa es localidades_a con filtro ambito → 'U' o 'R' (split en Python)."""
    table = str(getattr(layer, "table", "") or "")
    if not table.lower().endswith("localidades_a"):
        return None
    for col, vals in getattr(layer, "attr_filters", ()) or ():
        if str(col).lower() != "ambito" or not vals:
            continue
        v = str(vals[0]).strip().upper()
        if v.startswith("U"):
            return "U"
        if v.startswith("R"):
            return "R"
    return None


def _ambito_kind(raw: Any) -> str:
    """Clasifica ambito: 'U' urbana, 'R' rural, '?' desconocido/vacío."""
    s = str(raw or "").strip().upper()
    if not s:
        return "?"
    # Códigos INEGI frecuentes en mgn.localidades_a
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits in ("1", "01") or s in ("1", "01"):
        return "U"
    if digits in ("2", "02") or s in ("2", "02"):
        return "R"
    letters = "".join(ch for ch in s if ch.isalpha())
    if not letters:
        return "?"
    if letters.startswith("U") or "URBAN" in letters:
        return "U"
    if letters.startswith("R") or "RURAL" in letters:
        return "R"
    # Un solo carácter U/R
    if letters in ("U", "R"):
        return letters
    return "?"


def _ambito_matches_want(raw: Any, want: str) -> bool:
    """Clasifica ambito tolerando U/R, URBANA/RURAL y códigos 1/2."""
    kind = _ambito_kind(raw)
    if want == "U":
        return kind == "U"
    if want == "R":
        return kind == "R"
    return True


def _srid_from_settings() -> tuple[str, int]:
    settings = get_cartography_settings()
    map_crs = settings["map_crs"]
    srid = int(str(map_crs).split(":")[-1])
    return map_crs, srid


_MARCO_SRC_SRID: Optional[int] = None
_TABLE_SRC_SRID: dict[str, int] = {}


def _infer_srid_from_axis(ax: float, ay: float = 0.0) -> int:
    ax_a, ay_a = abs(ax), abs(ay)
    # Lon/lat
    if ax_a <= 180 and ay_a <= 90 and ax_a > 0:
        return 4326
    # Web Mercator México: X negativo ~ -1.1e7, Y ~ 1.5e6–2.5e6
    if ax < -1_000_000 and 500_000 < ay_a < 5_000_000:
        return 3857
    # México LCC (INEGI EPSG:6372/6362): false easting 2_500_000
    # Guerrero típico: X~2.5e6–2.9e6, Y~0.4e6–1.0e6
    if 2_000_000 < ax_a < 3_500_000 and 0 <= ay_a < 2_500_000:
        return 6372
    # UTM México (easting ~2e5–8e5, northing ~1.5e6–3e6)
    if 50_000 < ax_a < 2_000_000 and (ay_a == 0 or 500_000 < ay_a < 5_000_000):
        return 32614
    # WM genérico (|X| grande)
    if ax_a >= 5_000_000:
        return 3857
    return 3857


def _srid_tag_matches_coords(srid: int, ax: float, ay: float) -> bool:
    """Detecta SRID mentiroso (p.ej. UTM etiquetado como 3857)."""
    ax_a, ay_a = abs(ax), abs(ay)
    if srid == 4326:
        return ax_a <= 180 and ay_a <= 90
    if srid in (6362, 6372):
        # México LCC: X~2–3.5e6 (false easting 2.5e6), Y~0–2.5e6
        return 2_000_000 < ax_a < 3_500_000 and 0 <= ay_a < 2_500_000
    if srid in (32614, 32615, 32613, 6369):
        # UTM/métrico zona: easting ~2e5–8e5, northing ~1.5e6–3e6
        return 50_000 < ax_a < 2_000_000 and 500_000 < ay_a < 5_000_000
    if srid == 3857:
        # WM México: X negativo ~ -1e7 (no easting UTM ni LCC)
        if 50_000 < ax_a < 2_000_000 and 500_000 < ay_a < 5_000_000:
            return False  # parece UTM mal etiquetado
        if 2_000_000 < ax_a < 3_500_000 and ay_a < 2_500_000:
            return False  # parece México LCC mal etiquetado
        if ax_a < 1000 or ay_a < 1000:
            return False
        return True
    return True


def _resolve_src_srid(srid: int, ax: float, ay: float, *, label: str = "") -> int:
    """Elige SRID efectivo: confía en el tag solo si coincide con las coords."""
    if srid > 0 and _srid_tag_matches_coords(srid, ax, ay):
        return srid
    guessed = _infer_srid_from_axis(ax, ay)
    if srid > 0 and srid != guessed:
        log.warning(
            "SRID %s declarado=%s no coincide con coords (%.1f, %.1f) → usando %s",
            label or "?",
            srid,
            ax,
            ay,
            guessed,
        )
    else:
        log.info(
            "SRID %s era %s → asumido %s (xy=%.1f,%.1f)",
            label or "?",
            srid,
            guessed,
            ax,
            ay,
        )
    return guessed


def _safe_rollback(conn) -> None:
    """Tras un error SQL, limpiar la TX para poder seguir usando la conexión."""
    try:
        conn.rollback()
    except Exception:
        pass


def _source_srid_for_table(
    conn,
    schema: str,
    table: str,
    geom_col: str = "the_geom",
    *,
    cve_mun: Optional[str] = None,
    cve_loc: Optional[str] = None,
) -> int:
    """SRID real por tabla (crítico cuando ogr deja SRID=0 o SRID mentiroso).

    Si se pasan cve_mun/cve_loc, muestrea esa localidad (evita LIMIT 1 de otra zona
    con CRS distinto o coordenadas corruptas).
    """
    mun = _norm_cve3(cve_mun or "") if cve_mun else ""
    loc = _norm_cve4(cve_loc or "") if cve_loc else ""
    key = f"{schema}.{table}"
    if mun and loc:
        key = f"{key}:{mun}:{loc}"
    if key in _TABLE_SRC_SRID:
        return _TABLE_SRC_SRID[key]
    try:
        gcol = _safe_ident(geom_col)
        sch = _safe_ident(schema)
        tbl = _safe_ident(table)
        where = [f'g."{gcol}" IS NOT NULL']
        params: dict[str, Any] = {}
        # Preferir muestra de la misma localidad cuando existan las columnas.
        if mun:
            where.append("TRIM(BOTH FROM g.cve_mun::text) = %(mun)s")
            params["mun"] = mun
        if loc:
            where.append("TRIM(BOTH FROM g.cve_loc::text) = %(loc)s")
            params["loc"] = loc
        sql = f"""
            SELECT ST_SRID(g."{gcol}") AS srid,
                   ST_X(ST_Centroid(g."{gcol}")) AS x,
                   ST_Y(ST_Centroid(g."{gcol}")) AS y
              FROM "{sch}"."{tbl}" g
             WHERE {" AND ".join(where)}
             LIMIT 1
        """
        with conn.cursor() as cur:
            try:
                cur.execute(sql, params)
                row = cur.fetchone() or {}
            except Exception:
                # Tabla sin cve_mun/cve_loc o schema distinto → muestra global
                _safe_rollback(conn)
                cur.execute(
                    f"""
                    SELECT ST_SRID(g."{gcol}") AS srid,
                           ST_X(ST_Centroid(g."{gcol}")) AS x,
                           ST_Y(ST_Centroid(g."{gcol}")) AS y
                      FROM "{sch}"."{tbl}" g
                     WHERE g."{gcol}" IS NOT NULL
                     LIMIT 1
                    """
                )
                row = cur.fetchone() or {}
        srid = int(row.get("srid") or 0)
        ax = float(row.get("x") or 0.0)
        ay = float(row.get("y") or 0.0)
        if ax == 0.0 and ay == 0.0:
            return _probe_marco_source_srid(conn)
        resolved = _resolve_src_srid(srid, ax, ay, label=f"{schema}.{table}")
        _TABLE_SRC_SRID[key] = resolved
        return resolved
    except Exception:
        _safe_rollback(conn)
        log.exception("SRID %s.%s: fallback probe", schema, table)
        return _probe_marco_source_srid(conn)


def _probe_marco_source_srid(conn) -> int:
    """SRID de respaldo mirando varias tablas marco."""
    global _MARCO_SRC_SRID
    if _MARCO_SRC_SRID is not None:
        return _MARCO_SRC_SRID
    guessed = 3857
    try:
        with conn.cursor() as cur:
            for table in ("m", "pe", "e", "ea", "sil", "l"):
                try:
                    cur.execute(
                        f"""
                        SELECT ST_SRID(the_geom) AS srid,
                               ST_X(ST_Centroid(the_geom)) AS x,
                               ST_Y(ST_Centroid(the_geom)) AS y
                          FROM marco."{table}"
                         WHERE the_geom IS NOT NULL
                         LIMIT 1
                        """
                    )
                    row = cur.fetchone() or {}
                except Exception:
                    _safe_rollback(conn)
                    continue
                srid = int(row.get("srid") or 0)
                ax = float(row.get("x") or 0.0)
                ay = float(row.get("y") or 0.0)
                if ax == 0 and ay == 0:
                    continue
                if srid > 0 and _srid_tag_matches_coords(srid, ax, ay):
                    guessed = srid
                    break
                guessed = _infer_srid_from_axis(ax, ay)
                break
    except Exception:
        _safe_rollback(conn)
        log.exception("probe SRID marco falló; usando 3857")
        guessed = 3857
    _MARCO_SRC_SRID = guessed
    log.info("marco source SRID asumido/detectado: %s", guessed)
    return guessed


def _carto_default_src(
    conn,
    table: Optional[str] = None,
    *,
    schema: str = "marco",
    cve_mun: Optional[str] = None,
    cve_loc: Optional[str] = None,
) -> int:
    if table:
        return _source_srid_for_table(
            conn,
            schema or "marco",
            table,
            cve_mun=cve_mun,
            cve_loc=cve_loc,
        )
    return _probe_marco_source_srid(conn)


def _force_src_sql(geom_col: str, default_src: int) -> str:
    """
    Reetiqueta SRID sin reproyectar coordenadas.
    Necesario cuando ogr deja SRID mentiroso (p.ej. UTM marcado como 3857).
    """
    return f'ST_SetSRID(g."{geom_col}", {int(default_src)})'


def _safe_ident(name: str) -> str:
    cleaned = "".join(ch for ch in str(name) if ch.isalnum() or ch == "_")
    if not cleaned or cleaned != name:
        raise CartographyError("INVALID_COLUMN", f"Identificador inválido: {name}")
    return cleaned


def _qualify(schema: Optional[str], table: str) -> str:
    t = _safe_ident(table)
    if schema:
        return f'"{_safe_ident(schema)}"."{t}"'
    from tables import qualified

    return qualified(t)


def _db_cm(use_cartography: bool):
    if use_cartography:
        from database import get_cartography_db

        return get_cartography_db()
    from database import get_db

    return get_db()


def fetch_municipality(cve_mun: str) -> MunicipalityFeature:
    """Polígono municipal desde Atlas (c_mun)."""
    cve = _norm_cve3(cve_mun)
    if not cve:
        raise CartographyError("INVALID_CVE_MUN", "Se requiere cve_mun válido (3 dígitos)")

    from database import get_db
    from tables import T_MUN, qualified

    map_crs, srid = _srid_from_settings()
    sql = f"""
      SELECT
        TRIM(BOTH FROM cve_mun::text) AS cve_mun,
        TRIM(BOTH FROM COALESCE(nomgeo::text, '')) AS nomgeo,
        ST_AsGeoJSON(
          ST_Transform(
            CASE
              WHEN ST_SRID(the_geom) = 0 THEN ST_SetSRID(the_geom, 4326)
              ELSE the_geom
            END,
            %(srid)s
          )
        ) AS geojson
      FROM {qualified(T_MUN)}
      WHERE TRIM(BOTH FROM cve_mun::text) = %(cve)s
        AND the_geom IS NOT NULL
      LIMIT 1
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"cve": cve, "srid": srid})
            row = cur.fetchone()

    if not row:
        raise CartographyError(
            "MUNICIPIO_NOT_FOUND",
            f"Municipio no encontrado: {cve}",
            status_code=404,
        )
    geom = _parse_geojson(row.get("geojson"))
    if geom is None:
        raise CartographyError(
            "MUNICIPIO_NO_GEOM",
            f"Municipio {cve} sin geometría",
            status_code=404,
        )
    return MunicipalityFeature(
        cve_mun=str(row.get("cve_mun") or cve).strip(),
        nomgeo=str(row.get("nomgeo") or "").strip() or f"Municipio {cve}",
        geometry=geom,
        crs=map_crs,
    )


def fetch_localidad_area(
    *,
    cve_mun: str,
    cve_loc: str,
) -> Optional[Any]:
    """Polígono de localidad desde mgn.localidades_a (índice de armado)."""
    mun = _norm_cve3(cve_mun)
    loc = _norm_cve4(cve_loc)
    if not mun or not loc:
        return None
    _, srid = _srid_from_settings()
    sql = """
      SELECT ST_AsGeoJSON(
               ST_Transform(
                 CASE WHEN ST_SRID(the_geom)=0 THEN ST_SetSRID(the_geom,3857)
                      ELSE the_geom END,
                 %(srid)s
               )
             ) AS geojson
        FROM mgn.localidades_a
       WHERE TRIM(BOTH FROM cve_mun::text) = %(mun)s
         AND TRIM(BOTH FROM cve_loc::text) = %(loc)s
         AND the_geom IS NOT NULL
       LIMIT 1
    """
    try:
        with _db_cm(True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"mun": mun, "loc": loc, "srid": srid})
                row = cur.fetchone()
        if not row:
            return None
        return _parse_geojson(row.get("geojson"))
    except Exception:
        log.exception("fetch_localidad_area falló")
        return None


def fetch_municipality_cartography(cve_mun: str) -> MunicipalityFeature:
    """Polígono municipal desde GroSIG_Cartography (mgn.municipios_a / marco.mun)."""
    cve = _norm_cve3(cve_mun)
    if not cve:
        raise CartographyError("INVALID_CVE_MUN", "Se requiere cve_mun válido")

    map_crs, srid = _srid_from_settings()
    sql = """
      SELECT
        TRIM(BOTH FROM cve_mun::text) AS cve_mun,
        TRIM(BOTH FROM COALESCE(nomgeo::text, '')) AS nomgeo,
        ST_AsGeoJSON(
          ST_Transform(
            CASE WHEN ST_SRID(the_geom)=0 THEN ST_SetSRID(the_geom,3857) ELSE the_geom END,
            %(srid)s
          )
        ) AS geojson
      FROM mgn.municipios_a
      WHERE TRIM(BOTH FROM cve_mun::text) = %(cve)s
        AND the_geom IS NOT NULL
      LIMIT 1
    """
    with _db_cm(True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"cve": cve, "srid": srid})
            row = cur.fetchone()
    if not row:
        raise CartographyError(
            "MUNICIPIO_NOT_FOUND",
            f"Municipio no encontrado en cartography: {cve}",
            status_code=404,
        )
    geom = _parse_geojson(row.get("geojson"))
    if geom is None:
        raise CartographyError("MUNICIPIO_NO_GEOM", f"Municipio {cve} sin geometría", status_code=404)
    return MunicipalityFeature(
        cve_mun=str(row.get("cve_mun") or cve).strip(),
        nomgeo=str(row.get("nomgeo") or "").strip() or f"Municipio {cve}",
        geometry=geom,
        crs=map_crs,
        cve_ent="12",
        ent_nomgeo="Guerrero",
    )


def _map_extent_ctx_focus_sql(*, use_db_focus: bool) -> str:
    """Polígono foco en CRS del mapa (sin buffer).

    Preferir lectura desde ``mgn.municipios_a`` por ``exclude_cve`` para no
    mandar WKT gigante y mantener el mismo polígono que el resto del croquis.
    """
    if use_db_focus:
        return """(
          SELECT ST_MakeValid(ST_Transform(
            CASE WHEN ST_SRID(m.the_geom)=0 THEN ST_SetSRID(m.the_geom, 3857)
                 ELSE m.the_geom END,
            %(srid)s
          ))
            FROM mgn.municipios_a m
           WHERE TRIM(BOTH FROM m.cve_mun::text) = %(exclude_cve)s
             AND m.the_geom IS NOT NULL
           LIMIT 1
        )"""
    return "ST_MakeValid(ST_GeomFromText(%(focus_wkt)s, %(srid)s))"


def _prepare_map_extent_ctx(
    *,
    env: str,
    params: dict[str, Any],
    focus_geom: BaseGeometry,
    exclude_cve: str,
) -> tuple[str, list[str], str]:
    """Join + predicados ctx: lo que toca el extent y no está solo en el foco.

    Parciales OK (``ST_Intersects``). El PDF recorta al marco; no hacemos
    ``ST_Intersection`` en SQL (eso vaciaba capas / timeouts en 1.12.19).
    """
    use_db = bool(exclude_cve)
    if use_db:
        params["exclude_cve"] = exclude_cve
    else:
        params["focus_wkt"] = focus_geom.wkt
    focus_sql = _map_extent_ctx_focus_sql(use_db_focus=use_db)
    # Margen = envelope − foco (una sola vez). Sin Buffer negativo.
    join_sql = f"""
      CROSS JOIN LATERAL (
        SELECT ST_MakeValid({focus_sql}) AS focus_geom,
               ST_MakeValid(ST_Difference({env}, ST_MakeValid({focus_sql}))) AS margin_geom
      ) AS _ctx_margin
    """
    where_extra = [
        f"ST_Intersects({{g_map}}, {env})",
        (
            "_ctx_margin.margin_geom IS NOT NULL "
            "AND NOT ST_IsEmpty(_ctx_margin.margin_geom) "
            "AND ST_Intersects({g_map}, _ctx_margin.margin_geom)"
        ),
    ]
    # Ligero: distancia al borde del marco (sin ST_Intersection en ORDER BY).
    order_prefix = (
        f"ST_Distance({{g_map}}, ST_Boundary({env})) ASC NULLS LAST"
    )
    return join_sql, where_extra, order_prefix


def fetch_neighbor_municipality_cves(cve_mun: str) -> list[str]:
    """CVE de municipios que tocan/intersectan el foco (sin incluirlo)."""
    cve = _norm_cve3(cve_mun)
    if not cve:
        return []
    sql = """
      SELECT TRIM(BOTH FROM n.cve_mun::text) AS cve_mun
        FROM mgn.municipios_a n
        JOIN mgn.municipios_a f
          ON TRIM(BOTH FROM f.cve_mun::text) = %(cve)s
         AND f.the_geom IS NOT NULL
       WHERE n.the_geom IS NOT NULL
         AND TRIM(BOTH FROM n.cve_mun::text) <> %(cve)s
         AND ST_Intersects(
               CASE WHEN ST_SRID(n.the_geom)=0 THEN ST_SetSRID(n.the_geom,3857)
                    ELSE n.the_geom END,
               CASE WHEN ST_SRID(f.the_geom)=0 THEN ST_SetSRID(f.the_geom,3857)
                    ELSE f.the_geom END
             )
       ORDER BY 1
    """
    try:
        with _db_cm(True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"cve": cve})
                rows = cur.fetchall() or []
    except Exception:
        log.exception("fetch_neighbor_municipality_cves falló mun=%s", cve)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        n = _norm_cve3(str(row.get("cve_mun") or ""))
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def fetch_layers_in_bbox(
    layer_defs: list[LayerDef],
    *,
    cve_mun_list: Sequence[str] | None = None,
    bbox: tuple[float, float, float, float],
    exclude_cve_mun: Optional[str] = None,
    focus_geom: Optional[BaseGeometry] = None,
) -> list[LayerData]:
    """Capas de contexto en el **extent del mapa** (bbox visible).

    Con ``focus_geom``: todo lo que intersecta el marco y no es geometría del
    foco ya cubierta por capas foco (sin anillo/buffer alrededor del municipio).
    Sin ``focus_geom``: filtro por lista de vecinos / ``exclude_cve_mun`` + bbox.
    No trae municipios vecinos enteros: solo features que tocan el extent.
    """
    cves = [_norm_cve3(x) for x in (cve_mun_list or []) if _norm_cve3(x)]
    exclude = _norm_cve3(exclude_cve_mun or "")
    has_focus = focus_geom is not None and not getattr(focus_geom, "is_empty", True)
    if (not cves and not exclude and not has_focus) or not bbox or len(bbox) != 4:
        return [
            LayerData(definition=layer, geometry=None, feature_count=0)
            for layer in layer_defs
        ]
    out: list[LayerData] = []
    for layer in layer_defs:
        if not getattr(layer, "draw", True) and not layer.label_field:
            out.append(LayerData(definition=layer, geometry=None, feature_count=0))
            continue
        if not getattr(layer, "draw", True):
            out.append(LayerData(definition=layer, geometry=None, feature_count=0))
            continue
        try:
            data = fetch_layer(
                layer,
                # Con foco: el extent manda. Sin foco: lista vecinos o exclude.
                cve_mun_in=None if (has_focus or exclude) else (cves or None),
                exclude_cve_mun=exclude or None,
                bbox=bbox,
                clip_geom=None,
                focus_geom=focus_geom if has_focus else None,
            )
        except CartographyError:
            if layer.optional:
                data = LayerData(definition=layer, geometry=None, feature_count=0)
            else:
                raise
        except Exception:
            log.exception("fetch_layers_in_bbox %s falló", layer.id)
            data = LayerData(definition=layer, geometry=None, feature_count=0)
        out.append(data)
    return out


def fetch_labels_in_bbox(
    layer_defs: list[LayerDef],
    *,
    cve_mun_list: Sequence[str] | None = None,
    bbox: tuple[float, float, float, float],
    exclude_cve_mun: Optional[str] = None,
    focus_geom: Optional[BaseGeometry] = None,
) -> list[dict[str, Any]]:
    """Etiquetas de contexto acotadas al extent del mapa (misma regla que capas)."""
    cves = [_norm_cve3(x) for x in (cve_mun_list or []) if _norm_cve3(x)]
    exclude = _norm_cve3(exclude_cve_mun or "")
    has_focus = focus_geom is not None and not getattr(focus_geom, "is_empty", True)
    if (not cves and not exclude and not has_focus) or not bbox or len(bbox) != 4:
        return []
    out: list[dict[str, Any]] = []
    for layer in layer_defs:
        if not layer.label_field:
            continue
        try:
            labs = fetch_layer_labels(
                layer,
                cve_mun_in=None if (has_focus or exclude) else (cves or None),
                exclude_cve_mun=exclude or None,
                bbox=bbox,
                focus_geom=focus_geom if has_focus else None,
            )
        except Exception:
            log.exception("fetch_labels_in_bbox %s falló", layer.id)
            labs = []
        out.extend(labs)
    return out


def fetch_state_extent() -> MunicipalityFeature:
    """Extensión estatal desde mgn.estados_a (usa MunicipalityFeature como contenedor)."""
    map_crs, srid = _srid_from_settings()
    sql = """
      SELECT
        TRIM(BOTH FROM COALESCE(cve_ent::text, '12')) AS cve_mun,
        TRIM(BOTH FROM COALESCE(nomgeo::text, 'Guerrero')) AS nomgeo,
        ST_AsGeoJSON(
          ST_Transform(
            CASE WHEN ST_SRID(the_geom)=0 THEN ST_SetSRID(the_geom,3857) ELSE the_geom END,
            %(srid)s
          )
        ) AS geojson
      FROM mgn.estados_a
      WHERE the_geom IS NOT NULL
      LIMIT 1
    """
    with _db_cm(True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"srid": srid})
            row = cur.fetchone()
    if not row:
        raise CartographyError("ESTADO_NOT_FOUND", "Sin geometría de estado en mgn.estados_a", status_code=404)
    geom = _parse_geojson(row.get("geojson"))
    if geom is None:
        raise CartographyError("ESTADO_NO_GEOM", "Estado sin geometría", status_code=404)
    return MunicipalityFeature(
        cve_mun=str(row.get("cve_mun") or "12"),
        nomgeo=str(row.get("nomgeo") or "Guerrero"),
        geometry=geom,
        crs=map_crs,
    )


def fetch_locality(
    *,
    cve_mun: str,
    cve_loc: str,
    cve_ent: str = "12",
) -> LocalityFeature:
    """Localidad amanzanada (marco.l) en GroSIG_Cartography."""
    mun = _norm_cve3(cve_mun)
    loc = _norm_cve4(cve_loc)
    ent = _norm_cve2(cve_ent)
    if not mun or not loc:
        raise CartographyError(
            "INVALID_CVE_LOC",
            "Se requieren cve_mun (3) y cve_loc (4)",
        )

    map_crs, srid = _srid_from_settings()
    sql = """
      SELECT
        TRIM(BOTH FROM COALESCE(cve_ent::text, %(ent)s)) AS cve_ent,
        TRIM(BOTH FROM cve_mun::text) AS cve_mun,
        TRIM(BOTH FROM cve_loc::text) AS cve_loc,
        TRIM(BOTH FROM COALESCE(nomgeo::text, '')) AS nomgeo,
        TRIM(BOTH FROM COALESCE(ambito::text, '')) AS ambito,
        ST_AsGeoJSON(
          ST_Transform(
            CASE WHEN ST_SRID(the_geom)=0 THEN ST_SetSRID(the_geom,3857) ELSE the_geom END,
            %(srid)s
          )
        ) AS geojson
      FROM marco.l
      WHERE TRIM(BOTH FROM cve_mun::text) = %(mun)s
        AND TRIM(BOTH FROM cve_loc::text) = %(loc)s
        AND the_geom IS NOT NULL
      LIMIT 1
    """
    with _db_cm(True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"ent": ent, "mun": mun, "loc": loc, "srid": srid})
            row = cur.fetchone()
            mun_name = ""
            ent_name = ""
            if row:
                cur.execute(
                    """
                    SELECT TRIM(BOTH FROM COALESCE(nomgeo::text,'')) AS nomgeo
                      FROM mgn.municipios_a
                     WHERE TRIM(BOTH FROM cve_mun::text) = %(mun)s
                     LIMIT 1
                    """,
                    {"mun": mun},
                )
                r2 = cur.fetchone()
                mun_name = str((r2 or {}).get("nomgeo") or "")
                cur.execute(
                    """
                    SELECT TRIM(BOTH FROM COALESCE(nomgeo::text,'')) AS nomgeo
                      FROM mgn.estados_a
                     LIMIT 1
                    """
                )
                r3 = cur.fetchone()
                ent_name = str((r3 or {}).get("nomgeo") or "Guerrero")

    if not row:
        raise CartographyError(
            "LOCALIDAD_NOT_FOUND",
            f"Localidad amanzanada no encontrada: {mun}-{loc}",
            status_code=404,
        )
    geom = _parse_geojson(row.get("geojson"))
    if geom is None:
        raise CartographyError("LOCALIDAD_NO_GEOM", "Localidad sin geometría", status_code=404)

    ambito_raw = str(row.get("ambito") or "").strip().upper()
    if ambito_raw.startswith("U"):
        ambito = "Urbana"
    elif ambito_raw.startswith("R"):
        ambito = "Rural"
    else:
        ambito = ambito_raw or "Urbana"

    return LocalityFeature(
        cve_ent=str(row.get("cve_ent") or ent).strip(),
        cve_mun=str(row.get("cve_mun") or mun).strip(),
        cve_loc=str(row.get("cve_loc") or loc).strip(),
        nomgeo=str(row.get("nomgeo") or "").strip() or f"Localidad {loc}",
        ambito=ambito,
        geometry=geom,
        crs=map_crs,
        mun_nomgeo=mun_name or f"Municipio {mun}",
        ent_nomgeo=ent_name or "Guerrero",
    )


def list_municipalities() -> list[MunicipalityRef]:
    from database import get_db
    from tables import T_MUN, qualified

    sql = f"""
      SELECT
        TRIM(BOTH FROM cve_mun::text) AS cve_mun,
        TRIM(BOTH FROM COALESCE(nomgeo::text, '')) AS nomgeo
      FROM {qualified(T_MUN)}
      WHERE cve_mun IS NOT NULL
        AND TRIM(BOTH FROM cve_mun::text) <> ''
      ORDER BY TRIM(BOTH FROM cve_mun::text) ASC
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall() or []

    out: list[MunicipalityRef] = []
    seen: set[str] = set()
    for row in rows:
        cve = _norm_cve3(str(row.get("cve_mun") or ""))
        if not cve or cve in seen:
            continue
        seen.add(cve)
        out.append(
            MunicipalityRef(
                cve_mun=cve,
                nomgeo=str(row.get("nomgeo") or "").strip() or f"Municipio {cve}",
            )
        )
    return out


def _merge_geoms(geoms: list[BaseGeometry]) -> Optional[BaseGeometry]:
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    try:
        # Líneas: NO usar unary_union (puede absorber/eliminar tramos cortos del SIL).
        line_types = {"LineString", "MultiLineString"}
        if all(getattr(g, "geom_type", "") in line_types for g in geoms):
            parts: list[LineString] = []
            for g in geoms:
                if isinstance(g, LineString):
                    if not g.is_empty:
                        parts.append(g)
                elif isinstance(g, MultiLineString):
                    for p in g.geoms:
                        if isinstance(p, LineString) and not p.is_empty:
                            parts.append(p)
            if not parts:
                return None
            return parts[0] if len(parts) == 1 else MultiLineString(parts)
        return unary_union(geoms)
    except Exception:
        return GeometryCollection(geoms)


def fetch_layer(
    layer: LayerDef,
    *,
    cve_mun: Optional[str] = None,
    cve_loc: Optional[str] = None,
    clip_geom: Optional[BaseGeometry] = None,
    cve_mun_in: Optional[Sequence[str]] = None,
    exclude_cve_mun: Optional[str] = None,
    bbox: Optional[tuple[float, float, float, float]] = None,
    focus_geom: Optional[BaseGeometry] = None,
) -> LayerData:
    if layer.table not in ALLOWED_LAYER_TABLES:
        raise CartographyError("LAYER_TABLE_FORBIDDEN", f"Tabla no permitida: {layer.table}")

    from column_resolver import resolve_column
    from tables import SCHEMA, T_MUN, qualified

    schema, table_name = parse_table_ref(layer.table)
    use_carto = is_cartography_table(layer.table)
    resolve_schema = schema if use_carto else SCHEMA
    map_crs, srid = _srid_from_settings()
    table_sql = _qualify(schema, table_name) if use_carto else qualified(table_name)
    cve = _norm_cve3(cve_mun) if cve_mun else ""
    loc = _norm_cve4(cve_loc) if cve_loc else ""
    post_ambito_want = _localidades_a_ambito_want(layer)
    rows: list[Any] = []
    amb_raw: Optional[str] = None

    try:
        with _db_cm(use_carto) as conn:
            # SRID origen: por tabla (3857 / UTM / México LCC 6372).
            default_src = (
                _carto_default_src(
                    conn,
                    table_name,
                    schema=schema or "marco",
                    cve_mun=cve or None,
                    cve_loc=loc or None,
                )
                if use_carto
                else 4326
            )
            geom_col_raw = resolve_column(
                conn,
                resolve_schema,
                table_name,
                (layer.geom_column, "the_geom", "geom", "geometry", "wkb_geometry"),
            )
            if not geom_col_raw:
                if layer.optional:
                    return LayerData(definition=layer, geometry=None, feature_count=0)
                raise CartographyError(
                    "LAYER_NO_GEOM_COL",
                    f"Capa {layer.id}: sin columna de geometría",
                )
            geom_col = _safe_ident(geom_col_raw)

            where = [f'g."{geom_col}" IS NOT NULL']
            params: dict[str, Any] = {"srid": srid, "lim": int(layer.limit)}

            cve_list = [
                _norm_cve3(x) for x in (cve_mun_in or []) if _norm_cve3(x)
            ]
            exclude_cve = _norm_cve3(exclude_cve_mun or "")
            # Contexto croquis: extent del mapa (no anillo/buffer alrededor del foco).
            use_map_extent_ctx = (
                focus_geom is not None
                and not getattr(focus_geom, "is_empty", True)
                and bbox is not None
                and len(bbox) == 4
            )
            cve_expr: Optional[str] = None
            if exclude_cve or cve_list:
                cve_col_raw = resolve_column(
                    conn, resolve_schema, table_name, ("cve_mun", "CVE_MUN")
                )
                if not cve_col_raw:
                    if not use_map_extent_ctx:
                        if layer.optional:
                            return LayerData(
                                definition=layer, geometry=None, feature_count=0
                            )
                        raise CartographyError(
                            "LAYER_NO_CVE_COL",
                            f"Capa {layer.id}: tabla sin columna cve_mun",
                        )
                else:
                    cve_col = _safe_ident(cve_col_raw)
                    cve_expr = f'TRIM(BOTH FROM g."{cve_col}"::text)'
                    if not use_map_extent_ctx:
                        if exclude_cve:
                            # Incluir cve NULL/vacío (en SQL, NULL <> x es UNKNOWN).
                            where.append(
                                f"(NULLIF({cve_expr}, '') IS NULL "
                                f"OR {cve_expr} <> %(exclude_cve)s)"
                            )
                            params["exclude_cve"] = exclude_cve
                        else:
                            where.append(f"{cve_expr} = ANY(%(cves)s)")
                            params["cves"] = cve_list
                    elif exclude_cve:
                        params["exclude_cve"] = exclude_cve
            elif layer.filter_cve_mun:
                if not cve:
                    if layer.optional:
                        return LayerData(definition=layer, geometry=None, feature_count=0)
                    raise CartographyError("MISSING_CVE_MUN", f"Capa {layer.id} requiere cve_mun")
                cve_col_raw = resolve_column(
                    conn, resolve_schema, table_name, ("cve_mun", "CVE_MUN")
                )
                if not cve_col_raw:
                    if layer.optional:
                        return LayerData(definition=layer, geometry=None, feature_count=0)
                    raise CartographyError(
                        "LAYER_NO_CVE_COL",
                        f"Capa {layer.id}: tabla sin columna cve_mun",
                    )
                cve_col = _safe_ident(cve_col_raw)
                where.append(f'TRIM(BOTH FROM g."{cve_col}"::text) = %(cve)s')
                params["cve"] = cve

            force_loc_spatial = False
            sil_loc_expand = False
            if layer.filter_cve_loc and not cve_list and not exclude_cve and not use_map_extent_ctx:
                if not loc:
                    if layer.optional:
                        return LayerData(definition=layer, geometry=None, feature_count=0)
                    raise CartographyError("MISSING_CVE_LOC", f"Capa {layer.id} requiere cve_loc")
                loc_col_raw = resolve_column(
                    conn, resolve_schema, table_name, ("cve_loc", "CVE_LOC")
                )
                # SIL: además de cve_loc, incluir tramos del mismo mun que toquen
                # el polígono de localidad (recupera corrientes “perdidas” mal codificadas).
                if (
                    use_carto
                    and cve
                    and loc
                    and (
                        table_name == "sil"
                        or str(layer.id or "").startswith("sil_")
                    )
                ):
                    sil_loc_expand = True
                    params["loc"] = loc
                elif loc_col_raw:
                    loc_col = _safe_ident(loc_col_raw)
                    where.append(f'TRIM(BOTH FROM g."{loc_col}"::text) = %(loc)s')
                    params["loc"] = loc
                elif use_carto and cve and loc:
                    # marco.ea (atlas c_e) no trae cve_loc: filtrar por polígono de localidad.
                    # NUNCA devolver todo el municipio (revienta tiempo/memoria).
                    force_loc_spatial = True
                    params["loc"] = loc
                elif layer.optional:
                    return LayerData(definition=layer, geometry=None, feature_count=0)
                else:
                    raise CartographyError(
                        "LAYER_NO_LOC_COL",
                        f"Capa {layer.id}: tabla sin cve_loc",
                    )

            join_sql = ""
            use_bbox_mode = (
                bool(cve_list)
                or bool(exclude_cve)
                or use_map_extent_ctx
                or (bbox is not None and len(bbox) == 4)
            )
            if layer.clip_to_municipio and not use_carto and not use_bbox_mode:
                if not cve:
                    if layer.optional:
                        return LayerData(definition=layer, geometry=None, feature_count=0)
                    raise CartographyError("MISSING_CVE_MUN", f"Capa {layer.id} requiere cve_mun")
                join_sql = f"""
                  JOIN {qualified(T_MUN)} mun
                    ON TRIM(BOTH FROM mun.cve_mun::text) = %(cve)s
                   AND mun.the_geom IS NOT NULL
                """
                where.append(
                    f"""ST_Intersects(
                      CASE WHEN ST_SRID(g."{geom_col}") = 0 THEN ST_SetSRID(g."{geom_col}", {default_src})
                           ELSE g."{geom_col}" END,
                      CASE WHEN ST_SRID(mun.the_geom) = 0 THEN ST_SetSRID(mun.the_geom, 4326)
                           ELSE mun.the_geom END
                    )"""
                )
                params["cve"] = cve
            elif layer.clip_to_municipio and use_carto and not use_bbox_mode:
                if not cve:
                    if layer.optional:
                        return LayerData(definition=layer, geometry=None, feature_count=0)
                    raise CartographyError("MISSING_CVE_MUN", f"Capa {layer.id} requiere cve_mun")
                join_sql = """
                  JOIN mgn.municipios_a mun
                    ON TRIM(BOTH FROM mun.cve_mun::text) = %(cve)s
                   AND mun.the_geom IS NOT NULL
                """
                where.append(
                    f"""ST_Intersects(
                      CASE WHEN ST_SRID(g."{geom_col}") = 0 THEN ST_SetSRID(g."{geom_col}", {default_src})
                           ELSE g."{geom_col}" END,
                      CASE WHEN ST_SRID(mun.the_geom) = 0 THEN ST_SetSRID(mun.the_geom, 3857)
                           ELSE mun.the_geom END
                    )"""
                )
                params["cve"] = cve
            elif (
                (layer.clip_to_localidad or force_loc_spatial or sil_loc_expand)
                and use_carto
                and not use_bbox_mode
            ):
                if not cve or not loc:
                    if layer.optional:
                        return LayerData(definition=layer, geometry=None, feature_count=0)
                    raise CartographyError(
                        "MISSING_CVE_LOC",
                        f"Capa {layer.id} requiere cve_mun y cve_loc para clip",
                    )
                # Preferir polígono de área (mgn via marco.l). Buffer pequeño por si L es línea.
                join_sql = """
                  JOIN marco.l locpoly
                    ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(cve)s
                   AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
                   AND locpoly.the_geom IS NOT NULL
                """
                if sil_loc_expand:
                    loc_col_sil = _safe_ident(loc_col_raw) if loc_col_raw else "cve_loc"
                    g_src = _force_src_sql(geom_col, default_src)
                    where.append(
                        f"""(
                          TRIM(BOTH FROM g."{loc_col_sil}"::text) = %(loc)s
                          OR ST_DWithin(
                            {g_src},
                            CASE WHEN ST_SRID(locpoly.the_geom)=0
                                 THEN ST_SetSRID(locpoly.the_geom, {int(default_src)})
                                 ELSE locpoly.the_geom END,
                            120.0
                          )
                        )"""
                    )
                elif str(layer.id or "") == "colindantes":
                    # AGEB rurales adyacentes: meter en 3857 para DWithin en metros
                    g_raw = f"""CASE WHEN ST_SRID(g."{geom_col}")=0
                                    THEN ST_SetSRID(g."{geom_col}", {int(default_src)})
                                    ELSE g."{geom_col}" END"""
                    l_raw = f"""CASE WHEN ST_SRID(locpoly.the_geom)=0
                                    THEN ST_SetSRID(locpoly.the_geom, {int(default_src)})
                                    ELSE locpoly.the_geom END"""
                    where.append(
                        f"""ST_DWithin(
                          ST_Transform({g_raw}, 3857),
                          ST_Transform({l_raw}, 3857),
                          1800.0
                        )"""
                    )
                else:
                    where.append(
                        f"""ST_Intersects(
                          CASE WHEN ST_SRID(g."{geom_col}") = 0 THEN ST_SetSRID(g."{geom_col}", {default_src})
                               ELSE g."{geom_col}" END,
                          ST_Buffer(
                            CASE WHEN ST_SRID(locpoly.the_geom) = 0 THEN ST_SetSRID(locpoly.the_geom, 3857)
                                 ELSE locpoly.the_geom END,
                            25.0
                          )
                        )"""
                    )
                params["cve"] = cve
                params["loc"] = loc

            for af_i, (af_col, af_vals) in enumerate(getattr(layer, "attr_filters", ()) or ()):
                # localidades_a + ambito: NUNCA filtrar en SQL (tabla o want).
                # En BD a menudo viene como 1/2 (no 'Urbana') → LIKE 'U%' vacía la capa.
                # Se clasifica en Python con _ambito_kind (letras + códigos).
                if str(af_col).lower() == "ambito" and (
                    post_ambito_want
                    or str(table_name or "").lower().endswith("localidades_a")
                ):
                    continue
                af_raw = resolve_column(
                    conn,
                    resolve_schema,
                    table_name,
                    (af_col, af_col.upper(), af_col.lower()),
                )
                if not af_raw:
                    if layer.optional:
                        # Clasificación por código: sin columna → capa vacía (no duplicar todo)
                        if str(af_col).lower() in ("codigo_m", "codigo"):
                            return LayerData(
                                definition=layer, geometry=None, feature_count=0
                            )
                        # Otros filtros (p.ej. ambito): omitir filtro si no existe
                        continue
                    continue
                col_id = _safe_ident(af_raw)
                key = f"af_{af_i}"
                expr = f'UPPER(TRIM(BOTH FROM g."{col_id}"::text))'
                af_up = [str(v).strip().upper() for v in af_vals]
                if af_col.lower() == "ambito" and af_up and af_up[0].startswith("U"):
                    # Defensa: letras U* + códigos INEGI 1/01 (por si algún path aún filtra en SQL).
                    where.append(
                        f"({expr} LIKE 'U%' OR {expr} IN ('1','01') "
                        f"OR {expr} = ANY(%({key})s))"
                    )
                    params[key] = af_up
                elif af_col.lower() == "ambito" and af_up and af_up[0].startswith("R"):
                    where.append(
                        f"({expr} LIKE 'R%' OR {expr} IN ('2','02') "
                        f"OR {expr} = ANY(%({key})s))"
                    )
                    params[key] = af_up
                elif len(af_vals) == 1:
                    where.append(f"{expr} = %({key})s")
                    params[key] = af_up[0]
                else:
                    where.append(f"{expr} = ANY(%({key})s)")
                    params[key] = af_up

            ambito_select_sql = "NULL::text AS ambito_val"
            if post_ambito_want:
                amb_raw = resolve_column(
                    conn,
                    resolve_schema,
                    table_name,
                    ("ambito", "AMBITO"),
                )
                if amb_raw:
                    ambito_select_sql = (
                        f'TRIM(BOTH FROM g."{_safe_ident(amb_raw)}"::text) AS ambito_val'
                    )
                # Si el LIMIT es bajo, oversamping: el filtro Python de ambito
                # corre DESPUÉS del LIMIT (evita quedarse con pocas urbanas).
                if int(layer.limit) < 1500:
                    params["lim"] = min(50000, max(int(layer.limit) * 8, 3000))
                else:
                    params["lim"] = min(50000, int(layer.limit))

            for ex_i, (ex_col, ex_vals) in enumerate(
                getattr(layer, "attr_excludes", ()) or ()
            ):
                ex_raw = resolve_column(
                    conn,
                    resolve_schema,
                    table_name,
                    (ex_col, ex_col.upper(), ex_col.lower()),
                )
                if not ex_raw:
                    continue
                col_id = _safe_ident(ex_raw)
                key = f"ex_{ex_i}"
                expr = f'UPPER(TRIM(BOTH FROM g."{col_id}"::text))'
                if len(ex_vals) == 1:
                    where.append(f"{expr} <> %({key})s")
                    params[key] = str(ex_vals[0]).strip().upper()
                else:
                    where.append(f"NOT ({expr} = ANY(%({key})s))")
                    params[key] = [str(v).strip().upper() for v in ex_vals]

            order_sql = ""
            if bbox is not None and len(bbox) == 4:
                params["minx"] = float(bbox[0])
                params["miny"] = float(bbox[1])
                params["maxx"] = float(bbox[2])
                params["maxy"] = float(bbox[3])
                g_map = f"ST_Transform({_force_src_sql(geom_col, default_src)}, %(srid)s)"
                env = (
                    "ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, %(srid)s)"
                )
                where.append(f"{g_map} && {env}")
                if use_map_extent_ctx:
                    ctx_join, ctx_where, order_prefix = _prepare_map_extent_ctx(
                        env=env,
                        params=params,
                        focus_geom=focus_geom,  # type: ignore[arg-type]
                        exclude_cve=exclude_cve,
                    )
                    join_sql = f"{join_sql}\n{ctx_join}"
                    for pred in ctx_where:
                        where.append(pred.format(g_map=g_map))
                    order_sql = f" ORDER BY {order_prefix.format(g_map=g_map)}"

            # Preferir filas del ambito pedido antes del LIMIT (letras + códigos 1/2)
            if post_ambito_want and amb_raw and not order_sql:
                amb_expr = (
                    f'UPPER(TRIM(BOTH FROM g."{_safe_ident(amb_raw)}"::text))'
                )
                if post_ambito_want == "U":
                    order_sql = (
                        f" ORDER BY CASE WHEN {amb_expr} LIKE 'U%' "
                        f"OR {amb_expr} IN ('1','01') THEN 0 ELSE 1 END"
                    )
                elif post_ambito_want == "R":
                    order_sql = (
                        f" ORDER BY CASE WHEN {amb_expr} LIKE 'R%' "
                        f"OR {amb_expr} IN ('2','02') THEN 0 ELSE 1 END"
                    )

            geom_expr = f"""ST_Transform(
                         {_force_src_sql(geom_col, default_src)},
                         %(srid)s
                       )"""
            # Ctx: sin simplify (tramos del margen). El PDF recorta al marco.
            if layer.simplify and layer.simplify > 0 and not use_map_extent_ctx:
                params["simp"] = float(layer.simplify)
                geom_expr = f"ST_SimplifyPreserveTopology({geom_expr}, %(simp)s)"
            where_sql = " AND ".join(where)
            # WKB: menos CPU/IO que GeoJSON (crítico con miles de vértices).
            # localidades_a: también GeoJSON de respaldo (WKB a veces no parsea bien).
            want_geojson_backup = table_name.lower().endswith("localidades_a")
            if want_geojson_backup:
                sql = f"""
                  SELECT ST_AsBinary({geom_expr}) AS wkb,
                         ST_AsGeoJSON({geom_expr}) AS geojson,
                         {ambito_select_sql}
                    FROM {table_sql} g
                    {join_sql}
                   WHERE {where_sql}
                   {order_sql}
                   LIMIT %(lim)s
                """
            else:
                sql = f"""
                  SELECT ST_AsBinary({geom_expr}) AS wkb,
                         {ambito_select_sql}
                    FROM {table_sql} g
                    {join_sql}
                   WHERE {where_sql}
                   {order_sql}
                   LIMIT %(lim)s
                """
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
    except CartographyError:
        raise
    except Exception as exc:
        if layer.optional:
            log.exception(
                "fetch_layer ctx/optional vacío layer=%s: %s", layer.id, exc
            )
            return LayerData(definition=layer, geometry=None, feature_count=0)
        raise CartographyError(
            "LAYER_QUERY_FAILED",
            f"Error consultando capa {layer.id}: {exc}",
            status_code=500,
        ) from exc

    geoms: list[BaseGeometry] = []
    # Ambito solo en Python (códigos 1/2 + U/R). Si el estricto deja 0, excluir solo rurales.
    # Si no hay columna ambito pero la capa la pide, NO descartar (traer todas).
    if post_ambito_want and amb_raw:
        strict: list[BaseGeometry] = []
        soft: list[BaseGeometry] = []
        for row in rows:
            g = _parse_row_geom(row)
            if g is None:
                continue
            kind = _ambito_kind(row.get("ambito_val"))
            if post_ambito_want == "U":
                if kind == "U":
                    strict.append(g)
                if kind != "R":
                    soft.append(g)
            elif post_ambito_want == "R":
                if kind == "R":
                    strict.append(g)
                if kind != "U":
                    soft.append(g)
            else:
                strict.append(g)
                soft.append(g)
        if strict:
            geoms = strict
        else:
            geoms = soft
            log.warning(
                "fetch_layer %s: ambito estricto=0; fallback sin opuestos kept=%s raw=%s",
                getattr(layer, "id", ""),
                len(geoms),
                len(rows),
            )
    else:
        for row in rows:
            g = _parse_row_geom(row)
            if g is None:
                continue
            geoms.append(g)

    if post_ambito_want or str(getattr(layer, "id", "") or "").startswith("localidades"):
        log.info(
            "fetch_layer %s want=%s kept=%s raw_rows=%s amb_col=%s",
            getattr(layer, "id", ""),
            post_ambito_want,
            len(geoms),
            len(rows),
            bool(amb_raw),
        )

    # Localidades de área: armar MultiPolygon SIN unary_union (puede vaciar).
    loc_area_ids = (
        "localidades_urbana",
        "localidades_rural",
        "ctx_localidades_urbana",
        "ctx_localidades_rural",
        "ctx_localidades_a",
    )
    if str(getattr(layer, "id", "") or "") in loc_area_ids:
        try:
            from shapely.geometry import MultiPolygon, Polygon

            polys: list[Polygon] = []
            for g in geoms:
                if g is None or getattr(g, "is_empty", True):
                    continue
                gt = getattr(g, "geom_type", "")
                if gt == "Polygon":
                    polys.append(g)  # type: ignore[arg-type]
                elif gt == "MultiPolygon":
                    polys.extend(
                        [p for p in g.geoms if isinstance(p, Polygon) and not p.is_empty]
                    )
            if not polys:
                return LayerData(definition=layer, geometry=None, feature_count=0)
            merged = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        except Exception:
            merged = GeometryCollection(geoms) if geoms else None
            if merged is None or getattr(merged, "is_empty", True):
                return LayerData(definition=layer, geometry=None, feature_count=0)
    else:
        merged = _merge_geoms(geoms)
        if merged is None:
            return LayerData(definition=layer, geometry=None, feature_count=0)

    # AGEB (urbano / rural): si vienen como polígonos, dibujar BOUNDARIES
    # por feature (no unary_union de polígonos: disuelve y borra límites internos).
    if (
        layer.id in ("ageb", "ageb_rural")
        and merged is not None
        and not getattr(merged, "is_empty", True)
    ):
        try:
            boundaries: list[BaseGeometry] = []
            for g in geoms:
                if g is None or getattr(g, "is_empty", True):
                    continue
                gt = getattr(g, "geom_type", "")
                if gt in ("Polygon", "MultiPolygon"):
                    b = getattr(g, "boundary", None)
                    if b is not None and not b.is_empty:
                        boundaries.append(b)
                else:
                    boundaries.append(g)
            if boundaries:
                merged = _merge_geoms(boundaries) or merged
        except Exception:
            pass

    # clip_geom (marco.l) NO debe aplicarse a capas de contexto exterior
    # ni a SIL/ejes (el intersection corta tramos fuera del borde y “pierde” elementos).
    exterior_ids = frozenset(
        {
            "pe",
            "cd",
            "poligono_envolvente",
            "caserio",
            "caserio_disperso",
            "localidad",
            "localidades_urbana",
            "localidades_rural",
            "localidades_p",
            "ejes",
            "eje",
            "sil",
            "sil_carretera",
            "sil_canal",
            "sil_corriente",
            "sia",
            # AGEB rural: filtrada por cve_mun; el clip municipal puede vaciar
            # boundaries si hay micro-desfase topológico con el polígono mun.
            "ageb_rural",
        }
    )
    do_clip = (
        clip_geom is not None
        and not clip_geom.is_empty
        and not layer.clip_to_municipio
        and not layer.clip_to_localidad
        and layer.id not in exterior_ids
        and not str(layer.id or "").startswith("sil_")
        and not str(layer.id or "").startswith("sia_")
        and not str(layer.id or "").startswith("ctx_")
        and not (bbox is not None and len(bbox) == 4)
        and not str(layer.table or "").endswith((".pe", ".cd", ".sil", ".sia", ".e", ".ea", ".l"))
    )
    if do_clip:
        try:
            merged = merged.intersection(clip_geom)
        except Exception:
            pass

    if merged is None or merged.is_empty:
        return LayerData(definition=layer, geometry=None, feature_count=0)

    return LayerData(definition=layer, geometry=merged, feature_count=len(geoms))


def fetch_template_layers(
    layer_defs: list[LayerDef],
    *,
    cve_mun: Optional[str] = None,
    cve_loc: Optional[str] = None,
    clip_geom: Optional[BaseGeometry] = None,
    bbox: Optional[tuple[float, float, float, float]] = None,
) -> list[LayerData]:
    from dataclasses import replace

    out: list[LayerData] = []
    for layer in layer_defs:
        # Solo etiquetas: no consultar ni pintar geometría (ahorra tiempo)
        if not getattr(layer, "draw", True):
            out.append(LayerData(definition=layer, geometry=None, feature_count=0))
            continue
        data = fetch_layer(
            layer,
            cve_mun=cve_mun,
            cve_loc=cve_loc,
            clip_geom=clip_geom,
            bbox=bbox,
        )
        if (
            layer.id == "ejes"
            and (data.geometry is None or getattr(data.geometry, "is_empty", True))
        ):
            alt_table = "marco.e" if layer.table == "marco.ea" else "marco.ea"
            data = fetch_layer(
                replace(layer, table=alt_table),
                cve_mun=cve_mun,
                cve_loc=cve_loc,
                clip_geom=clip_geom,
                bbox=bbox,
            )
        # PE/CD: vecindad espacial a marco.l de ESTA localidad
        if layer.table in ("marco.pe", "marco.cd") and cve_mun and cve_loc:
            near = fetch_layer_near_localidad(
                layer, cve_mun=cve_mun, cve_loc=cve_loc
            )
            if layer.table == "marco.cd":
                # CD capa: solo near espacial. Etiquetas/puntos vienen de fetch_cd_labeled_points.
                if near.geometry is not None and not near.geometry.is_empty:
                    data = near
                else:
                    data = LayerData(
                        definition=layer, geometry=None, feature_count=0
                    )
            elif data.geometry is None:
                data = near
            elif near.geometry is not None and not near.geometry.is_empty:
                merged = _merge_geoms(
                    [g for g in (data.geometry, near.geometry) if g is not None]
                )
                if merged is not None and not merged.is_empty:
                    data = LayerData(
                        definition=layer,
                        geometry=merged,
                        feature_count=max(
                            int(data.feature_count or 0), int(near.feature_count or 0)
                        ),
                    )
        # AGEB rurales colindantes: si aux.colindantes vacío, probar mgn / otras L
        if layer.id == "colindantes" and cve_mun and cve_loc:
            if data.geometry is None or getattr(data.geometry, "is_empty", True):
                data = fetch_colindantes_near_localidad(
                    layer, cve_mun=cve_mun, cve_loc=cve_loc
                )
        out.append(data)
    return out


def fetch_cd_labeled_points(
    *,
    cve_mun: str,
    cve_loc: str,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """
    Caserío disperso (marco.cd) con etiqueta.
    Texto: cve_mza → últimos 3 de cvegeo → '·'
    """
    from column_resolver import resolve_column

    mun = _norm_cve3(cve_mun)
    loc = _norm_cve4(cve_loc)
    if not mun or not loc:
        return []

    _, srid = _srid_from_settings()
    default_src = 3857
    out: list[dict[str, Any]] = []
    try:
        with _db_cm(True) as conn:
            geom_col_raw = resolve_column(
                conn, "marco", "cd", ("the_geom", "geom", "geometry", "wkb_geometry")
            )
            if not geom_col_raw:
                return []
            geom_col = _safe_ident(geom_col_raw)
            mza_col = resolve_column(
                conn, "marco", "cd", ("cve_mza", "CVE_MZA", "mza", "MZA", "clave")
            )
            geo_col = resolve_column(
                conn, "marco", "cd", ("cvegeo", "CVEGEO", "cve_geo", "CVE_GEO")
            )
            cve_col = resolve_column(conn, "marco", "cd", ("cve_mun", "CVE_MUN"))
            loc_col = resolve_column(conn, "marco", "cd", ("cve_loc", "CVE_LOC"))

            coalesce_parts: list[str] = []
            if mza_col:
                coalesce_parts.append(
                    f"NULLIF(TRIM(BOTH FROM g.\"{_safe_ident(mza_col)}\"::text), '')"
                )
            if geo_col:
                gc = _safe_ident(geo_col)
                coalesce_parts.append(
                    f"NULLIF(RIGHT(TRIM(BOTH FROM g.\"{gc}\"::text), 3), '')"
                )
            coalesce_parts.append("'·'")
            label_expr = "COALESCE(" + ", ".join(coalesce_parts) + ")"

            g_raw = f"""CASE WHEN ST_SRID(g."{geom_col}")=0
                              THEN ST_SetSRID(g."{geom_col}",{default_src})
                              ELSE g."{geom_col}" END"""
            l_raw = """CASE WHEN ST_SRID(locpoly.the_geom)=0
                             THEN ST_SetSRID(locpoly.the_geom,3857)
                             ELSE locpoly.the_geom END"""
            g_m = f"ST_Transform({g_raw}, 3857)"
            l_m = f"ST_Transform({l_raw}, 3857)"

            params: dict[str, Any] = {
                "srid": srid,
                "lim": int(limit),
                "mun": mun,
                "loc": loc,
            }
            where_extra = ""
            if cve_col:
                where_extra += (
                    f' AND TRIM(BOTH FROM g."{_safe_ident(cve_col)}"::text) = %(mun)s'
                )

            rows: list = []
            # ST_Dimension=0 → Point/MultiPoint (Dump de CADA punto).
            # Ojo: GeometryType() a veces devuelve 'POINT' sin prefijo ST_ →
            # si cae en PointOnSurface de un MultiPoint, colapsa a 1 solo punto.
            dump_expr = f"""ST_Dump(
              CASE
                WHEN ST_Dimension({g_raw}) = 0 THEN {g_raw}
                ELSE ST_PointOnSurface({g_raw})
              END
            )"""
            pe_m = """ST_Transform(
              CASE WHEN ST_SRID(pe.the_geom)=0 THEN ST_SetSRID(pe.the_geom,3857)
                   ELSE pe.the_geom END, 3857)"""
            loc_filter = ""
            pe_loc_col = resolve_column(conn, "marco", "pe", ("cve_loc", "CVE_LOC"))
            if pe_loc_col:
                loc_filter = (
                    f' AND TRIM(BOTH FROM pe."{_safe_ident(pe_loc_col)}"::text) = %(loc)s'
                )
            # CD de la localidad = dentro/borde del PE de esta L (ref. QGIS: 6 pts en el PE)
            sql_near = f"""
              SELECT
                {label_expr} AS label_text,
                ST_AsGeoJSON(ST_Transform(dp.geom, %(srid)s)) AS geojson
              FROM marco.cd g
              JOIN marco.l locpoly
                ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(mun)s
               AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
               AND locpoly.the_geom IS NOT NULL
              JOIN marco.pe pe
                ON pe.the_geom IS NOT NULL
               AND TRIM(BOTH FROM pe.cve_mun::text) = %(mun)s
               {loc_filter}
               AND ST_Intersects({pe_m}, {l_m})
              CROSS JOIN LATERAL {dump_expr} AS dp
              WHERE g."{geom_col}" IS NOT NULL
                {where_extra}
                AND ST_DWithin({g_m}, {pe_m}, 80)
              LIMIT %(lim)s
            """
            with conn.cursor() as cur:
                cur.execute(sql_near, params)
                rows = cur.fetchall() or []

            # Fallback: cve_loc atributo + cerca de L (si PE no enlazó)
            if not rows and loc_col:
                sql_attr = f"""
                  SELECT
                    {label_expr} AS label_text,
                    ST_AsGeoJSON(ST_Transform(dp.geom, %(srid)s)) AS geojson
                  FROM marco.cd g
                  JOIN marco.l locpoly
                    ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(mun)s
                   AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
                   AND locpoly.the_geom IS NOT NULL
                  CROSS JOIN LATERAL {dump_expr} AS dp
                  WHERE g."{geom_col}" IS NOT NULL
                    {where_extra}
                    AND TRIM(BOTH FROM g."{_safe_ident(loc_col)}"::text) = %(loc)s
                    AND ST_DWithin({g_m}, {l_m}, 2500)
                  LIMIT %(lim)s
                """
                with conn.cursor() as cur:
                    cur.execute(sql_attr, params)
                    rows = cur.fetchall() or []

            if not rows:
                sql_l = f"""
                  SELECT
                    {label_expr} AS label_text,
                    ST_AsGeoJSON(ST_Transform(dp.geom, %(srid)s)) AS geojson
                  FROM marco.cd g
                  JOIN marco.l locpoly
                    ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(mun)s
                   AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
                   AND locpoly.the_geom IS NOT NULL
                  CROSS JOIN LATERAL {dump_expr} AS dp
                  WHERE g."{geom_col}" IS NOT NULL
                    {where_extra}
                    AND ST_DWithin({g_m}, {l_m}, 1500)
                  LIMIT %(lim)s
                """
                with conn.cursor() as cur:
                    cur.execute(sql_l, params)
                    rows = cur.fetchall() or []
    except Exception:
        log.exception("fetch_cd_labeled_points falló")
        return []

    seen: set[tuple[float, float]] = set()
    for row in rows:
        geom = _parse_geojson(row.get("geojson"))
        if geom is None:
            continue
        try:
            if geom.geom_type == "Point":
                key = (round(float(geom.x), 3), round(float(geom.y), 3))
            else:
                p = geom.representative_point()
                key = (round(float(p.x), 3), round(float(p.y), 3))
                from shapely.geometry import Point as _Pt

                geom = _Pt(float(p.x), float(p.y))
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        raw_txt = str(row.get("label_text") or "").strip()
        if raw_txt.isdigit():
            # Conservar "800" (no colapsar con int() a "800" ok, pero "080"→80)
            text = raw_txt[-3:] if len(raw_txt) >= 3 else raw_txt
        else:
            text = raw_txt
        if text in ("", "·", ".", "0", "000"):
            text = ""
        out.append({"geometry": geom, "text": text[:12]})
    return out


def cd_features_from_geometry(geom: Any, default_text: str = "·") -> list[dict[str, Any]]:
    """Convierte geometría CD (capa) en lista de puntos etiquetables."""
    from shapely.geometry import Point

    if geom is None or getattr(geom, "is_empty", True):
        return []
    pts: list[Any] = []
    try:
        gtype = getattr(geom, "geom_type", "")
        if gtype == "Point":
            pts = [geom]
        elif gtype == "MultiPoint":
            pts = list(geom.geoms)
        elif gtype.startswith("Multi") or gtype == "GeometryCollection":
            for part in geom.geoms:
                if part.geom_type == "Point":
                    pts.append(part)
                else:
                    try:
                        p = part.representative_point()
                        pts.append(Point(p.x, p.y))
                    except Exception:
                        continue
        else:
            p = geom.representative_point()
            pts.append(Point(p.x, p.y))
    except Exception:
        return []
    return [{"geometry": p, "text": default_text} for p in pts if p is not None]


def fetch_colindantes_near_localidad(
    layer: LayerDef,
    *,
    cve_mun: str,
    cve_loc: str,
) -> LayerData:
    """
    AGEB rurales colindantes cerca de la localidad urbana.
    Orden: aux.colindantes → mgn.ageb_rurales_a → marco.a (otras cve_loc del mun).
    """
    from column_resolver import resolve_column
    from dataclasses import replace

    mun = _norm_cve3(cve_mun)
    loc = _norm_cve4(cve_loc)
    if not mun or not loc:
        return LayerData(definition=layer, geometry=None, feature_count=0)

    _, map_srid = _srid_from_settings()
    candidates = (
        ("aux", "colindantes", False),
        ("mgn", "ageb_rurales_a", False),
        ("marco", "a", True),  # otras localidades del mismo mun
    )
    for schema, table, exclude_loc in candidates:
        try:
            with _db_cm(True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM information_schema.tables
                         WHERE table_schema=%(s)s AND table_name=%(t)s
                         LIMIT 1
                        """,
                        {"s": schema, "t": table},
                    )
                    if not cur.fetchone():
                        continue
                geom_col_raw = resolve_column(
                    conn, schema, table, ("the_geom", "geom", "geometry", "wkb_geometry")
                )
                if not geom_col_raw:
                    continue
                geom_col = _safe_ident(geom_col_raw)
                src = _carto_default_src(conn, table, cve_mun=mun, cve_loc=loc) if schema == "marco" else 3857
                # sample SRID for mgn/aux
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            f'SELECT ST_SRID("{geom_col}") AS s FROM {schema}.{table} WHERE "{geom_col}" IS NOT NULL LIMIT 1'
                        )
                        r = cur.fetchone() or {}
                        srid_row = int(r.get("s") or 0)
                        if srid_row > 0:
                            src = srid_row
                except Exception:
                    _safe_rollback(conn)

                g_raw = f"""CASE WHEN ST_SRID(g."{geom_col}")=0
                                THEN ST_SetSRID(g."{geom_col}", {int(src)})
                                ELSE g."{geom_col}" END"""
                l_raw = """CASE WHEN ST_SRID(locpoly.the_geom)=0
                                THEN ST_SetSRID(locpoly.the_geom, 3857)
                                ELSE locpoly.the_geom END"""
                extra = ""
                params: dict[str, Any] = {
                    "mun": mun,
                    "loc": loc,
                    "srid": map_srid,
                    "lim": max(50, min(int(layer.limit or 400), 2000)),
                }
                if exclude_loc:
                    extra = "AND TRIM(BOTH FROM g.cve_loc::text) <> %(loc)s"
                # filtro mun si existe columna
                mun_filter = ""
                mun_col = resolve_column(conn, schema, table, ("cve_mun", "CVE_MUN"))
                if mun_col:
                    mun_filter = f'AND TRIM(BOTH FROM g."{_safe_ident(mun_col)}"::text) = %(mun)s'

                sql = f"""
                  SELECT ST_AsGeoJSON(ST_Transform({g_raw}, %(srid)s)) AS geojson
                    FROM {schema}.{table} g
                    JOIN marco.l locpoly
                      ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(mun)s
                     AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
                     AND locpoly.the_geom IS NOT NULL
                   WHERE g."{geom_col}" IS NOT NULL
                     {mun_filter}
                     {extra}
                     AND ST_DWithin(
                       ST_Transform({g_raw}, 3857),
                       ST_Transform({l_raw}, 3857),
                       1800.0
                     )
                   LIMIT %(lim)s
                """
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall() or []
            geoms = []
            for row in rows:
                g = _parse_geojson(row.get("geojson"))
                if g is not None and not g.is_empty:
                    # poligonos → boundary para trazo
                    gt = getattr(g, "geom_type", "")
                    if gt in ("Polygon", "MultiPolygon"):
                        b = getattr(g, "boundary", None)
                        if b is not None and not b.is_empty:
                            geoms.append(b)
                        else:
                            geoms.append(g)
                    else:
                        geoms.append(g)
            merged = _merge_geoms(geoms)
            if merged is not None and not merged.is_empty:
                log.info(
                    "colindantes: %s features desde %s.%s (mun=%s loc=%s)",
                    len(geoms),
                    schema,
                    table,
                    mun,
                    loc,
                )
                return LayerData(
                    definition=replace(layer, table=f"{schema}.{table}"),
                    geometry=merged,
                    feature_count=len(geoms),
                )
        except Exception:
            log.exception("fetch_colindantes_near %s.%s falló", schema, table)
            continue
    return LayerData(definition=layer, geometry=None, feature_count=0)


def fetch_layer_near_localidad(
    layer: LayerDef,
    *,
    cve_mun: str,
    cve_loc: str,
) -> LayerData:
    """
    PE / CD cuando no hay match por cve_loc (o la columna no existe):
    toma features del municipio que intersectan / están cerca de marco.l.
    """
    from column_resolver import resolve_column

    mun = _norm_cve3(cve_mun)
    loc = _norm_cve4(cve_loc)
    if not mun or not loc:
        return LayerData(definition=layer, geometry=None, feature_count=0)

    schema, table_name = parse_table_ref(layer.table)
    if schema != "marco":
        return LayerData(definition=layer, geometry=None, feature_count=0)

    _, srid = _srid_from_settings()
    default_src = 3857
    table_sql = _qualify(schema, table_name)

    try:
        with _db_cm(True) as conn:
            geom_col_raw = resolve_column(
                conn,
                schema,
                table_name,
                (layer.geom_column, "the_geom", "geom", "geometry", "wkb_geometry"),
            )
            if not geom_col_raw:
                return LayerData(definition=layer, geometry=None, feature_count=0)
            geom_col = _safe_ident(geom_col_raw)
            cve_col_raw = resolve_column(conn, schema, table_name, ("cve_mun", "CVE_MUN"))
            cve_filter = ""
            params: dict[str, Any] = {
                "srid": srid,
                "lim": int(layer.limit),
                "mun": mun,
                "loc": loc,
            }
            if cve_col_raw:
                cve_filter = f'AND TRIM(BOTH FROM g."{_safe_ident(cve_col_raw)}"::text) = %(mun)s'

            g_raw = f"""CASE WHEN ST_SRID(g."{geom_col}")=0
                              THEN ST_SetSRID(g."{geom_col}",{default_src})
                              ELSE g."{geom_col}" END"""
            l_raw = """CASE WHEN ST_SRID(locpoly.the_geom)=0
                             THEN ST_SetSRID(locpoly.the_geom,3857)
                             ELSE locpoly.the_geom END"""
            # Misma proyección métrica antes de Intersects/DWithin (evita SRID cruzado)
            g_m = f"ST_Transform({g_raw}, 3857)"
            l_m = f"ST_Transform({l_raw}, 3857)"

            if layer.id == "pe" or table_name == "pe":
                spatial = (
                    f"(ST_Intersects({g_m}, {l_m}) OR ST_Contains({g_m}, {l_m})"
                    f" OR ST_Contains({l_m}, {g_m}))"
                )
            else:
                # CD capa: dentro/borde del PE de esta localidad (como en QGIS)
                pe_m = """ST_Transform(
                  CASE WHEN ST_SRID(pe.the_geom)=0 THEN ST_SetSRID(pe.the_geom,3857)
                       ELSE pe.the_geom END, 3857)"""
                pe_loc_col = resolve_column(conn, schema, "pe", ("cve_loc", "CVE_LOC"))
                loc_filter = ""
                if pe_loc_col:
                    loc_filter = (
                        f' AND TRIM(BOTH FROM pe."{_safe_ident(pe_loc_col)}"::text) = %(loc)s'
                    )
                spatial = f"""EXISTS (
                  SELECT 1 FROM marco.pe pe
                   WHERE pe.the_geom IS NOT NULL
                     AND TRIM(BOTH FROM pe.cve_mun::text) = %(mun)s
                     {loc_filter}
                     AND ST_Intersects({pe_m}, {l_m})
                     AND ST_DWithin({g_m}, {pe_m}, 80)
                )"""

            sql = f"""
              SELECT ST_AsGeoJSON(ST_Transform({g_raw}, %(srid)s)) AS geojson
                FROM {table_sql} g
                JOIN marco.l locpoly
                  ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(mun)s
                 AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
                 AND locpoly.the_geom IS NOT NULL
               WHERE g."{geom_col}" IS NOT NULL
                 {cve_filter}
                 AND {spatial}
               LIMIT %(lim)s
            """
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
    except Exception:
        log.exception("fetch_layer_near_localidad %s falló", layer.id)
        return LayerData(definition=layer, geometry=None, feature_count=0)

    geoms: list[BaseGeometry] = []
    for row in rows:
        g = _parse_geojson(row.get("geojson"))
        if g is not None:
            geoms.append(g)
    merged = _merge_geoms(geoms)
    if merged is None or merged.is_empty:
        return LayerData(definition=layer, geometry=None, feature_count=0)
    return LayerData(definition=layer, geometry=merged, feature_count=len(geoms))


def _merge_line_geoms(geoms: list[BaseGeometry]) -> Optional[BaseGeometry]:
    """Une tramos de la misma vialidad/SIL en una sola geometría de línea."""
    parts: list[LineString] = []
    for g in geoms:
        if g is None or getattr(g, "is_empty", True):
            continue
        if isinstance(g, LineString):
            parts.append(g)
        elif isinstance(g, MultiLineString):
            parts.extend(
                p for p in g.geoms if isinstance(p, LineString) and not p.is_empty
            )
        elif isinstance(g, GeometryCollection):
            for sub in g.geoms:
                if isinstance(sub, LineString) and not sub.is_empty:
                    parts.append(sub)
                elif isinstance(sub, MultiLineString):
                    parts.extend(
                        p
                        for p in sub.geoms
                        if isinstance(p, LineString) and not p.is_empty
                    )
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    try:
        from shapely.ops import linemerge

        u = unary_union(parts)
        if isinstance(u, LineString):
            return u
        if isinstance(u, MultiLineString):
            merged = linemerge(u)
            return merged if merged is not None and not merged.is_empty else u
        return MultiLineString(parts)
    except Exception:
        return MultiLineString(parts)


def _line_label_components(
    geoms: list[BaseGeometry],
    *,
    min_length: float = 20.0,
) -> list[LineString]:
    """
    Componentes conexos para etiquetar: 1 etiqueta por tramo continuo
    (no una sola para todo el municipio con el mismo nombre).
    """
    merged = _merge_line_geoms(geoms)
    if merged is None or merged.is_empty:
        return []
    lines: list[LineString] = []
    if isinstance(merged, LineString):
        lines = [merged]
    elif isinstance(merged, MultiLineString):
        lines = [p for p in merged.geoms if isinstance(p, LineString) and not p.is_empty]
    out = [ln for ln in lines if float(ln.length) >= float(min_length)]
    if out:
        return sorted(out, key=lambda ln: ln.length, reverse=True)
    # Si todos son cortos, conservar el más largo
    if lines:
        return [max(lines, key=lambda ln: ln.length)]
    return []


def _select_spaced_line_components(
    components: list[LineString],
    *,
    max_labels: int = 2,
    min_separation: float = 3000.0,
) -> list[LineString]:
    """Deja los tramos más largos y evita etiquetas del mismo nombre demasiado juntas.

    ``min_separation`` en unidades del CRS de mapa (m en UTM/3857).
    """
    if not components:
        return []
    max_n = max(1, int(max_labels))
    # 1 etiqueta cada ~8 km de red (estado: ~4 km), tope max_n
    total = sum(float(ln.length) for ln in components)
    chunk = 4000.0 if max_n >= 4 else 8000.0
    by_len = max(1, min(max_n, int(total / chunk) + 1))
    budget = min(max_n, by_len)
    sep = float(min_separation)
    if total > 0:
        # Al menos ~28% (o ~18% si hay presupuesto alto) entre dos del mismo nombre
        sep = max(sep, total * (0.18 if max_n >= 4 else 0.28))

    kept: list[LineString] = []
    mids: list[BaseGeometry] = []
    for comp in sorted(components, key=lambda ln: float(ln.length), reverse=True):
        if len(kept) >= budget:
            break
        mid, _ang = _line_midpoint_angle(comp)
        if mid is None:
            continue
        too_close = False
        for prev in mids:
            try:
                if float(mid.distance(prev)) < sep:
                    too_close = True
                    break
            except Exception:
                continue
        if too_close:
            continue
        kept.append(comp)
        mids.append(mid)
    return kept


def _line_midpoint_angle(geom: BaseGeometry) -> tuple[Optional[BaseGeometry], float]:
    """Punto medio + ángulo (grados) para etiqueta paralela a una línea."""
    import math

    line = None
    if isinstance(geom, LineString) and not geom.is_empty:
        line = geom
    elif isinstance(geom, MultiLineString) and not geom.is_empty:
        parts = [g for g in geom.geoms if isinstance(g, LineString) and g.length > 0]
        if parts:
            line = max(parts, key=lambda g: g.length)
    if line is None or line.length <= 0:
        try:
            return geom.representative_point(), 0.0
        except Exception:
            return None, 0.0
    try:
        mid = line.interpolate(0.5, normalized=True)
        # Rumbo sobre el tramo central (evita esquinas y ventanas demasiado cortas
        # que dejan el texto transversal al sentido visual de la corriente/calle).
        p0 = line.interpolate(0.28, normalized=True)
        p1 = line.interpolate(0.72, normalized=True)
        dx, dy = p1.x - p0.x, p1.y - p0.y
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return mid, 0.0
        ang = math.degrees(math.atan2(dy, dx))
        if ang > 90.0:
            ang -= 180.0
        elif ang < -90.0:
            ang += 180.0
        return mid, float(ang)
    except Exception:
        try:
            return line.interpolate(0.5, normalized=True), 0.0
        except Exception:
            return None, 0.0


def fetch_layer_labels(
    layer: LayerDef,
    *,
    cve_mun: Optional[str] = None,
    cve_loc: Optional[str] = None,
    cve_mun_in: Optional[Sequence[str]] = None,
    exclude_cve_mun: Optional[str] = None,
    bbox: Optional[tuple[float, float, float, float]] = None,
    focus_geom: Optional[BaseGeometry] = None,
) -> list[dict[str, Any]]:
    if not layer.label_field:
        return []
    if layer.table not in ALLOWED_LAYER_TABLES:
        return []

    from column_resolver import resolve_column
    from tables import SCHEMA, qualified

    schema, table_name = parse_table_ref(layer.table)
    use_carto = is_cartography_table(layer.table)
    resolve_schema = schema if use_carto else SCHEMA
    cve = _norm_cve3(cve_mun) if cve_mun else ""
    loc = _norm_cve4(cve_loc) if cve_loc else ""
    _, srid = _srid_from_settings()
    table_sql = _qualify(schema, table_name) if use_carto else qualified(table_name)
    post_ambito_want = _localidades_a_ambito_want(layer)

    label_candidates = (
        layer.label_field,
        "nom_loc",
        "nomgeo",
        "nombre",
        "name",
        "nomvial",
        "NOMVIAL",
        "geografico",
        "GEOGRAFICO",
        "cve_mza",
        "CVE_MZA",
        "cve_ageb",
        "CVE_AGEB",
    )

    try:
        with _db_cm(use_carto) as conn:
            default_src = (
                _carto_default_src(
                    conn,
                    table_name,
                    schema=schema or "marco",
                    cve_mun=cve or None,
                    cve_loc=loc or None,
                )
                if use_carto
                else 4326
            )
            geom_col_raw = resolve_column(
                conn,
                resolve_schema,
                table_name,
                (layer.geom_column, "the_geom", "geom", "geometry"),
            )
            label_col_raw = resolve_column(
                conn, resolve_schema, table_name, label_candidates
            )
            if not geom_col_raw or not label_col_raw:
                return []
            geom_col = _safe_ident(geom_col_raw)
            label_col = _safe_ident(label_col_raw)

            prefix_col = None
            if layer.label_prefix_field:
                prefix_raw = resolve_column(
                    conn,
                    resolve_schema,
                    table_name,
                    (layer.label_prefix_field, "cve_mun", "cve_loc", "CVE_MUN", "CVE_LOC"),
                )
                if prefix_raw:
                    prefix_col = _safe_ident(prefix_raw)

            suffix_col = None
            if getattr(layer, "label_suffix_field", None):
                suffix_raw = resolve_column(
                    conn,
                    resolve_schema,
                    table_name,
                    (
                        layer.label_suffix_field,
                        "cvevial",
                        "CVEVIAL",
                        "cve_vial",
                        "CVE_VIAL",
                    ),
                )
                if suffix_raw:
                    suffix_col = _safe_ident(suffix_raw)

            where = [
                f'g."{geom_col}" IS NOT NULL',
                f"NULLIF(TRIM(BOTH FROM g.\"{label_col}\"::text), '') IS NOT NULL",
            ]
            params: dict[str, Any] = {
                "srid": srid,
                # Along: traer más tramos; luego se colapsan por componente
                "lim": max(
                    int(layer.label_limit),
                    200 if (getattr(layer, "label_along", False) or layer.label_style == "along") else int(layer.label_limit),
                ),
            }

            for ex_i, ex_val in enumerate(layer.label_exclude or ()):
                key = f"lex_{ex_i}"
                where.append(
                    f'UPPER(TRIM(BOTH FROM g."{label_col}"::text)) <> %({key})s'
                )
                params[key] = str(ex_val).strip().upper()

            cve_list = [
                _norm_cve3(x) for x in (cve_mun_in or []) if _norm_cve3(x)
            ]
            exclude_cve = _norm_cve3(exclude_cve_mun or "")
            use_map_extent_ctx = (
                focus_geom is not None
                and not getattr(focus_geom, "is_empty", True)
                and bbox is not None
                and len(bbox) == 4
            )
            cve_expr: Optional[str] = None
            if exclude_cve or cve_list:
                cve_col_raw = resolve_column(
                    conn, resolve_schema, table_name, ("cve_mun", "CVE_MUN")
                )
                if not cve_col_raw:
                    if not use_map_extent_ctx:
                        return []
                else:
                    cve_expr = f'TRIM(BOTH FROM g."{_safe_ident(cve_col_raw)}"::text)'
                    if not use_map_extent_ctx:
                        if exclude_cve:
                            where.append(
                                f"(NULLIF({cve_expr}, '') IS NULL "
                                f"OR {cve_expr} <> %(exclude_cve)s)"
                            )
                            params["exclude_cve"] = exclude_cve
                        else:
                            where.append(f"{cve_expr} = ANY(%(cves)s)")
                            params["cves"] = cve_list
                    elif exclude_cve:
                        params["exclude_cve"] = exclude_cve
            elif layer.filter_cve_mun:
                if not cve:
                    return []
                cve_col_raw = resolve_column(
                    conn, resolve_schema, table_name, ("cve_mun", "CVE_MUN")
                )
                if not cve_col_raw:
                    return []
                where.append(
                    f'TRIM(BOTH FROM g."{_safe_ident(cve_col_raw)}"::text) = %(cve)s'
                )
                params["cve"] = cve
            need_loc_join = False
            sil_loc_expand = (
                use_carto
                and bool(cve)
                and bool(loc)
                and (
                    table_name == "sil"
                    or str(layer.id or "").startswith("sil_")
                )
            )
            if layer.filter_cve_loc and not cve_list and not exclude_cve and not use_map_extent_ctx:
                if not loc:
                    return []
                loc_col_raw = resolve_column(
                    conn, resolve_schema, table_name, ("cve_loc", "CVE_LOC")
                )
                if sil_loc_expand and loc_col_raw:
                    need_loc_join = True
                    params["cve"] = cve
                    params["loc"] = loc
                    loc_id = _safe_ident(loc_col_raw)
                    g_src = _force_src_sql(geom_col, default_src)
                    where.append(
                        f"""(
                          TRIM(BOTH FROM g."{loc_id}"::text) = %(loc)s
                          OR ST_DWithin(
                            {g_src},
                            CASE WHEN ST_SRID(locpoly.the_geom)=0
                                 THEN ST_SetSRID(locpoly.the_geom, {int(default_src)})
                                 ELSE locpoly.the_geom END,
                            120.0
                          )
                        )"""
                    )
                elif loc_col_raw:
                    where.append(
                        f'TRIM(BOTH FROM g."{_safe_ident(loc_col_raw)}"::text) = %(loc)s'
                    )
                    params["loc"] = loc
                elif use_carto and cve:
                    need_loc_join = True
                    params["cve"] = cve
                    params["loc"] = loc
                    where.append(
                        f"""ST_Intersects(
                          CASE WHEN ST_SRID(g."{geom_col}")=0 THEN ST_SetSRID(g."{geom_col}",{default_src})
                               ELSE g."{geom_col}" END,
                          locpoly.the_geom
                        )"""
                    )
                else:
                    return []

            for af_i, (af_col, af_vals) in enumerate(getattr(layer, "attr_filters", ()) or ()):
                if str(af_col).lower() == "ambito" and (
                    post_ambito_want
                    or str(table_name or "").lower().endswith("localidades_a")
                ):
                    continue
                af_raw = resolve_column(
                    conn,
                    resolve_schema,
                    table_name,
                    (af_col, af_col.upper(), af_col.lower()),
                )
                if not af_raw:
                    # No abortar etiquetas: columna ausente → ignorar filtro
                    continue
                col_id = _safe_ident(af_raw)
                key = f"laf_{af_i}"
                expr = f'UPPER(TRIM(BOTH FROM g."{col_id}"::text))'
                af_up = [str(v).strip().upper() for v in af_vals]
                if af_col.lower() == "ambito" and af_up and af_up[0].startswith("U"):
                    where.append(
                        f"({expr} LIKE 'U%' OR {expr} IN ('1','01') "
                        f"OR {expr} = ANY(%({key})s))"
                    )
                    params[key] = af_up
                elif af_col.lower() == "ambito" and af_up and af_up[0].startswith("R"):
                    where.append(
                        f"({expr} LIKE 'R%' OR {expr} IN ('2','02') "
                        f"OR {expr} = ANY(%({key})s))"
                    )
                    params[key] = af_up
                elif len(af_vals) == 1:
                    where.append(f"{expr} = %({key})s")
                    params[key] = af_up[0]
                else:
                    where.append(f"{expr} = ANY(%({key})s)")
                    params[key] = af_up

            ambito_select_sql = "NULL::text AS ambito_val"
            if post_ambito_want:
                amb_raw = resolve_column(
                    conn, resolve_schema, table_name, ("ambito", "AMBITO")
                )
                if amb_raw:
                    ambito_select_sql = (
                        f'TRIM(BOTH FROM g."{_safe_ident(amb_raw)}"::text) AS ambito_val'
                    )

            for ex_i, (ex_col, ex_vals) in enumerate(
                getattr(layer, "attr_excludes", ()) or ()
            ):
                ex_raw = resolve_column(
                    conn,
                    resolve_schema,
                    table_name,
                    (ex_col, ex_col.upper(), ex_col.lower()),
                )
                if not ex_raw:
                    continue
                col_id = _safe_ident(ex_raw)
                key = f"lex_{ex_i}"
                expr = f'UPPER(TRIM(BOTH FROM g."{col_id}"::text))'
                if len(ex_vals) == 1:
                    where.append(f"{expr} <> %({key})s")
                    params[key] = str(ex_vals[0]).strip().upper()
                else:
                    where.append(f"NOT ({expr} = ANY(%({key})s))")
                    params[key] = [str(v).strip().upper() for v in ex_vals]

            join_sql = ""
            use_bbox_mode = (
                bool(cve_list)
                or bool(exclude_cve)
                or use_map_extent_ctx
                or (bbox is not None and len(bbox) == 4)
            )
            if (
                (layer.clip_to_localidad or need_loc_join)
                and use_carto
                and cve
                and loc
                and not use_bbox_mode
            ):
                join_sql = """
                  JOIN marco.l locpoly
                    ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(cve)s
                   AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
                   AND locpoly.the_geom IS NOT NULL
                """
                if layer.clip_to_localidad and not need_loc_join:
                    if str(layer.id or "") == "colindantes":
                        g_raw = f"""CASE WHEN ST_SRID(g."{geom_col}")=0
                                        THEN ST_SetSRID(g."{geom_col}",{default_src})
                                        ELSE g."{geom_col}" END"""
                        l_raw = f"""CASE WHEN ST_SRID(locpoly.the_geom)=0
                                        THEN ST_SetSRID(locpoly.the_geom,{default_src})
                                        ELSE locpoly.the_geom END"""
                        where.append(
                            f"""ST_DWithin(
                              ST_Transform({g_raw}, 3857),
                              ST_Transform({l_raw}, 3857),
                              1800.0
                            )"""
                        )
                    else:
                        where.append(
                            f"""ST_Intersects(
                              CASE WHEN ST_SRID(g."{geom_col}")=0 THEN ST_SetSRID(g."{geom_col}",{default_src})
                                   ELSE g."{geom_col}" END,
                              locpoly.the_geom
                            )"""
                        )
                params["cve"] = cve
                params["loc"] = loc

            extent_order_prefix = ""
            if bbox is not None and len(bbox) == 4:
                params["minx"] = float(bbox[0])
                params["miny"] = float(bbox[1])
                params["maxx"] = float(bbox[2])
                params["maxy"] = float(bbox[3])
                g_map = f"ST_Transform({_force_src_sql(geom_col, default_src)}, %(srid)s)"
                env = (
                    "ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, %(srid)s)"
                )
                where.append(f"{g_map} && {env}")
                if use_map_extent_ctx:
                    ctx_join, ctx_where, order_prefix = _prepare_map_extent_ctx(
                        env=env,
                        params=params,
                        focus_geom=focus_geom,  # type: ignore[arg-type]
                        exclude_cve=exclude_cve,
                    )
                    join_sql = f"{join_sql}\n{ctx_join}"
                    for pred in ctx_where:
                        where.append(pred.format(g_map=g_map))
                    extent_order_prefix = f"{order_prefix.format(g_map=g_map)}, "

            prefix_select = (
                f'TRIM(BOTH FROM g."{prefix_col}"::text) AS label_prefix,'
                if prefix_col
                else "NULL::text AS label_prefix,"
            )
            suffix_select = (
                f'TRIM(BOTH FROM g."{suffix_col}"::text) AS label_suffix,'
                if suffix_col
                else "NULL::text AS label_suffix,"
            )
            where_sql = " AND ".join(where)
            along = bool(getattr(layer, "label_along", False) or layer.label_style == "along")
            geom_expr = _force_src_sql(geom_col, default_src)
            is_ageb = (
                layer.label_style == "ageb_oval"
                or layer.id == "ageb"
                or (layer.label_field and "ageb" in str(layer.label_field).lower())
            )
            # Along: línea completa. AGEB: 1 punto por clave (collect). Resto: centroide.
            if along:
                shape_expr = geom_expr
                group_sql = ""
                order_sql = f"ORDER BY LENGTH(TRIM(BOTH FROM g.\"{label_col}\"::text)) ASC"
                select_label = f'TRIM(BOTH FROM g."{label_col}"::text) AS label_text'
            elif is_ageb:
                # Una etiqueta por AGEB (collect de tramos → centroide)
                shape_expr = f"ST_Centroid(ST_Collect({geom_expr}))"
                group_sql = f'GROUP BY TRIM(BOTH FROM g."{label_col}"::text)'
                order_sql = f"ORDER BY TRIM(BOTH FROM g.\"{label_col}\"::text) ASC"
                select_label = f'TRIM(BOTH FROM g."{label_col}"::text) AS label_text'
                prefix_select = "NULL::text AS label_prefix,"
                suffix_select = "NULL::text AS label_suffix,"
            else:
                # Localidades de área: centroide si cae dentro; si no, PointOnSurface.
                is_area_loc = str(layer.id or "") in (
                    "localidades_urbana",
                    "localidades_rural",
                    "ctx_localidades_a",
                    "ctx_localidades_urbana",
                    "ctx_localidades_rural",
                    "ctx_municipios",
                    "municipios",
                    "entidad",
                ) or str(layer.table or "").endswith(
                    ("localidades_a", "municipios_a", "estados_a")
                )
                if is_area_loc and str(layer.id or "") == "entidad" and bbox is not None and len(bbox) == 4:
                    # Preferir centro del mapa si cae dentro de la porción visible del estado.
                    g_map = f"ST_Transform({geom_expr}, %(srid)s)"
                    env = (
                        "ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, %(srid)s)"
                    )
                    vis = (
                        f"ST_CollectionExtract(ST_MakeValid(ST_Intersection({g_map}, {env})), 3)"
                    )
                    mid = (
                        "ST_SetSRID(ST_MakePoint("
                        "(%(minx)s + %(maxx)s) / 2.0, "
                        "(%(miny)s + %(maxy)s) / 2.0"
                        "), %(srid)s)"
                    )
                    shape_expr = (
                        f"CASE WHEN {vis} IS NULL OR ST_IsEmpty({vis}) THEN NULL "
                        f"WHEN ST_Within({mid}, {vis}) THEN {mid} "
                        f"WHEN ST_Within(ST_Centroid({vis}), {vis}) "
                        f"THEN ST_Centroid({vis}) "
                        f"ELSE ST_PointOnSurface({vis}) END"
                    )
                    geojson_expr = f"ST_AsGeoJSON({shape_expr})"
                elif is_area_loc and str(layer.id or "") in (
                    "ctx_municipios",
                    "ctx_localidades_a",
                    "ctx_localidades_urbana",
                    "ctx_localidades_rural",
                ) and bbox is not None and len(bbox) == 4:
                    # Centroide / PointOnSurface de la PORCIÓN VISIBLE (intersección con bbox del mapa)
                    g_map = f"ST_Transform({geom_expr}, %(srid)s)"
                    env = (
                        "ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, %(srid)s)"
                    )
                    vis = (
                        f"ST_CollectionExtract(ST_MakeValid(ST_Intersection({g_map}, {env})), 3)"
                    )
                    shape_expr = (
                        f"CASE WHEN {vis} IS NULL OR ST_IsEmpty({vis}) THEN NULL "
                        f"WHEN ST_Within(ST_Centroid({vis}), {vis}) "
                        f"THEN ST_Centroid({vis}) "
                        f"ELSE ST_PointOnSurface({vis}) END"
                    )
                    # Ya en CRS de mapa
                    geojson_expr = f"ST_AsGeoJSON({shape_expr})"
                elif is_area_loc:
                    shape_expr = (
                        f"CASE WHEN ST_Within(ST_Centroid({geom_expr}), {geom_expr}) "
                        f"THEN ST_Centroid({geom_expr}) "
                        f"ELSE ST_PointOnSurface({geom_expr}) END"
                    )
                    geojson_expr = f"ST_AsGeoJSON(ST_Transform({shape_expr}, %(srid)s))"
                else:
                    shape_expr = f"ST_Centroid({geom_expr})"
                    geojson_expr = f"ST_AsGeoJSON(ST_Transform({shape_expr}, %(srid)s))"
                group_sql = ""
                # Localidades de área: mayor área primero → 70–90% de etiquetas en las grandes.
                if str(layer.id or "") in (
                    "localidades_urbana",
                    "localidades_rural",
                    "ctx_localidades_urbana",
                    "ctx_localidades_rural",
                ):
                    order_sql = (
                        f"ORDER BY ST_Area({geom_expr}) DESC NULLS LAST"
                    )
                else:
                    order_sql = (
                        f"ORDER BY LENGTH(TRIM(BOTH FROM g.\"{label_col}\"::text)) ASC"
                    )
                select_label = f'TRIM(BOTH FROM g."{label_col}"::text) AS label_text'
            if along:
                geojson_expr = f"ST_AsGeoJSON(ST_Transform({shape_expr}, %(srid)s))"
            elif is_ageb:
                geojson_expr = f"ST_AsGeoJSON(ST_Transform({shape_expr}, %(srid)s))"
            if extent_order_prefix and not group_sql:
                # Priorizar etiquetas del margen del mapa cuando hay LIMIT.
                # (No combinar con GROUP BY: Postgres exige agregados.)
                if order_sql.upper().startswith("ORDER BY"):
                    order_sql = (
                        "ORDER BY "
                        + extent_order_prefix
                        + order_sql[len("ORDER BY") :].lstrip()
                    )
                else:
                    order_sql = "ORDER BY " + extent_order_prefix.rstrip(", ")
            sql = f"""
              SELECT
                {prefix_select}
                {suffix_select}
                {select_label},
                {ambito_select_sql},
                {geojson_expr} AS geojson
              FROM {table_sql} g
              {join_sql}
              WHERE {where_sql}
              {group_sql}
              {order_sql}
              LIMIT %(lim)s
            """
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
    except Exception:
        log.exception("labels %s: error consultando PostGIS", layer.id)
        return []

    raw_items: list[dict[str, Any]] = []
    label_rows = list(rows)
    if post_ambito_want:
        strict_rows = [
            r for r in label_rows if _ambito_matches_want(r.get("ambito_val"), post_ambito_want)
        ]
        if strict_rows:
            label_rows = strict_rows
        else:
            # Fallback: excluir el ámbito opuesto (p.ej. solo quitar rurales)
            opp = "R" if post_ambito_want == "U" else "U"
            label_rows = [
                r
                for r in label_rows
                if _ambito_kind(r.get("ambito_val")) != opp
            ]
            log.warning(
                "labels %s: ambito estricto=0; fallback kept=%s raw=%s",
                getattr(layer, "id", ""),
                len(label_rows),
                len(rows),
            )
    for row in label_rows:
        text = str(row.get("label_text") or "").strip()
        if not text:
            continue
        if text.upper() in set(layer.label_exclude or ()):
            continue
        if str(getattr(layer, "label_case", "") or "") == "title":
            from cartography_engine.text_format import to_proper_name

            text = to_proper_name(text)
        prefix = str(row.get("label_prefix") or "").strip()
        suffix = str(row.get("label_suffix") or "").strip()
        fmt = str(getattr(layer, "label_format", "") or "").strip().lower()
        if fmt == "paren":
            if suffix:
                text = f"{text} ({suffix})"
            elif prefix:
                text = f"{prefix} ({text})"
        elif fmt == "newline":
            # Clave en 1.ª línea; nombre debajo (hasta 3 renglones).
            _area_loc_ids = (
                "localidades_urbana",
                "localidades_rural",
                "ctx_localidades_urbana",
                "ctx_localidades_rural",
            )
            _mun_ids = (
                "municipios",
                "ctx_municipios",
                "entidad",
                "municipio",
            )
            if prefix and text and layer.id in _area_loc_ids + _mun_ids:
                from cartography_engine.text_format import format_localidad_area_label

                text = format_localidad_area_label(prefix, text)
            elif prefix and text:
                text = f"{prefix}\n{text}"
            elif prefix:
                text = prefix
            elif suffix and text:
                text = f"{text}\n{suffix}"
        elif prefix:
            text = f"{prefix} {text}".strip()
        elif suffix:
            text = f"{text} ({suffix})"
        is_mza = layer.id in ("manzanas", "manzana", "cd", "caserio", "caserio_disperso") or (
            layer.label_field and layer.label_field.lower() == "cve_mza"
        )
        if is_mza and text.isdigit():
            text = str(int(text))
        if layer.label_style == "ageb_oval" or (
            layer.label_field and "ageb" in layer.label_field.lower()
        ):
            formatted = format_ageb_clave(text)
            if formatted:
                text = formatted
            elif not text:
                continue
        geom = _parse_geojson(row.get("geojson"))
        if geom is None:
            continue
        raw_items.append({"text": text, "geometry": geom})

    along = bool(getattr(layer, "label_along", False) or layer.label_style == "along")
    labels: list[dict[str, Any]] = []
    if along:
        # SIL / líneas: 1 etiqueta por componente conexo (no una sola para todo
        # el mapa con el mismo nombre → evitaba etiquetar corrientes “perdidas”).
        groups: dict[str, list[BaseGeometry]] = {}
        text_by_key: dict[str, str] = {}
        for item in raw_items:
            key = str(item["text"]).strip().upper()
            groups.setdefault(key, []).append(item["geometry"])
            text_by_key[key] = item["text"]
        max_labs = max(1, int(getattr(layer, "label_limit", 40) or 40))
        is_sil = str(layer.id or "").startswith("sil_") or layer.table.endswith(".sil")
        is_hydro = str(layer.id or "") in (
            "corrientes",
            "ctx_corrientes",
        ) or str(layer.table or "").endswith("corrientes_agua_l")
        # Hidro croquis: tramos cortos no merecen etiqueta; SIL sí es denso.
        # Corrientes del condensado/estado: más permisivo (más hidrónimos).
        is_state_hydro = str(layer.id or "") in ("corrientes", "cuerpos")
        min_len = (
            8.0
            if is_sil
            else (55.0 if is_state_hydro else (180.0 if is_hydro else 25.0))
        )
        for key, geoms in groups.items():
            components = _line_label_components(geoms, min_length=min_len)
            if is_hydro:
                components = _select_spaced_line_components(
                    components,
                    max_labels=6 if is_state_hydro else 2,
                    min_separation=1600.0 if is_state_hydro else 3500.0,
                )
            for comp in components:
                if len(labels) >= max_labs:
                    break
                mid, angle = _line_midpoint_angle(comp)
                if mid is None:
                    continue
                labels.append(
                    {
                        "text": text_by_key[key],
                        "geometry": mid,
                        "layer_id": layer.id,
                        "color": layer.label_color,
                        "bold": layer.label_bold,
                        "italic": bool(getattr(layer, "label_italic", False)),
                        "size": layer.label_size,
                        "style": layer.label_style,
                        "anchor": layer.label_anchor,
                        "angle": angle,
                        # Solo SIL se aparta del trazo; ejes van centrados (offset 0).
                        "offset": (
                            1.85
                            if layer.id == "sil_carretera"
                            else (1.35 if is_sil else 0.0)
                        ),
                    }
                )
            if len(labels) >= max_labs:
                break
    else:
        # AGEB: dedupe por texto (una oval por clave)
        seen_ageb: set[str] = set()
        for item in raw_items:
            text = str(item["text"]).strip()
            if layer.label_style == "ageb_oval" or layer.id == "ageb":
                key = text.upper()
                if key in seen_ageb:
                    continue
                seen_ageb.add(key)
            labels.append(
                {
                    "text": item["text"],
                    "geometry": item["geometry"],
                    "layer_id": layer.id,
                    "color": layer.label_color,
                    "bold": layer.label_bold,
                    "italic": bool(getattr(layer, "label_italic", False)),
                    "size": layer.label_size,
                    "style": layer.label_style,
                    "anchor": layer.label_anchor,
                    "angle": 0.0,
                }
            )
            if len(labels) >= max(1, int(getattr(layer, "label_limit", 40) or 40)):
                break
    return labels


def format_vialidad_tipo_nom(
    *,
    nomvial: str = "",
    tipovial: str = "",
    text: str = "",
) -> str:
    """Formato urbano / cartas detalle: ``tipovial nomvial`` (sin cvevial)."""
    nom = str(nomvial or "").strip()
    tipo = str(tipovial or "").strip()
    if not nom:
        raw = str(text or "").strip()
        if "(" in raw:
            nom = raw.split("(", 1)[0].strip()
        else:
            nom = raw
    if not nom or nom.upper() == "NINGUNO":
        return ""
    if tipo and tipo.upper() not in ("NINGUNO", "NONE", "NULL"):
        # Evitar "CALLE CALLE X" si nomvial ya incluye el tipo
        if nom.upper().startswith(tipo.upper() + " "):
            return nom
        return f"{tipo} {nom}"
    return nom


def fetch_vialidad_labels(
    *,
    cve_mun: Optional[str] = None,
    cve_loc: Optional[str] = None,
    limit: int = 300,
    size: float = 4.8,
    text_mode: str = "paren",
) -> list[dict[str, Any]]:
    """
    Etiquetas vialidad.

    - ``text_mode=paren`` (PLR / default): ``nomvial (cvevial)``
    - ``text_mode=tipo_nom`` (PLU overview urbano): ``tipovial nomvial``
    - ``marco.e`` (GDB): suele traer cve_loc + cvevial
    - ``marco.ea`` (atlas c_e): tiene nomvial + cvegeo (sin cve_loc/cvevial);
      el código se toma de los últimos 5 dígitos de cvegeo; filtro espacial a localidad.
    - ``size``: tipografía en pt (plantilla urbana ~2.5; rural overview ~4.8).
    """
    from column_resolver import resolve_column

    mun = _norm_cve3(cve_mun or "")
    loc = _norm_cve4(cve_loc or "")
    if not mun or not loc:
        return []
    label_size = float(size) if size and float(size) > 0 else 4.8
    mode = str(text_mode or "paren").strip().lower()
    if mode in ("tipo_nom", "tiponom", "tipo+nom", "tipo_nomvial"):
        mode = "tipo_nom"
    else:
        mode = "paren"
    _, srid = _srid_from_settings()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # e primero (INEGI completo); ea = fallback atlas
    for table_name in ("e", "ea"):
        rows: list[Any] = []
        try:
            with _db_cm(True) as conn:
                # ¿Existe la tabla?
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM information_schema.tables
                         WHERE table_schema='marco' AND table_name=%(t)s
                         LIMIT 1
                        """,
                        {"t": table_name},
                    )
                    if not cur.fetchone():
                        continue

                # SRID con muestra de esta mun/loc (no LIMIT 1 global)
                default_src = _carto_default_src(
                    conn, table_name, cve_mun=mun, cve_loc=loc
                )
                log.info(
                    "fetch_vialidad_labels marco.%s src_srid=%s map=%s mun=%s loc=%s",
                    table_name,
                    default_src,
                    srid,
                    mun,
                    loc,
                )

                geom_col_raw = resolve_column(
                    conn, "marco", table_name, ("the_geom", "geom", "geometry", "wkb_geometry")
                )
                nom_col = resolve_column(
                    conn,
                    "marco",
                    table_name,
                    ("nomvial", "NOMVIAL", "nom_vial", "nombre", "name"),
                )
                cve_col = resolve_column(
                    conn,
                    "marco",
                    table_name,
                    ("cvevial", "CVEVIAL", "cve_vial", "CVE_VIAL"),
                )
                cvegeo_col = resolve_column(
                    conn, "marco", table_name, ("cvegeo", "CVEGEO", "cve_geo")
                )
                mun_col = resolve_column(
                    conn, "marco", table_name, ("cve_mun", "CVE_MUN")
                )
                loc_col = resolve_column(
                    conn, "marco", table_name, ("cve_loc", "CVE_LOC")
                )
                tipovial_col = resolve_column(
                    conn,
                    "marco",
                    table_name,
                    ("tipovial", "TIPOVIAL", "tipo_vial", "tvial"),
                )
                if not geom_col_raw or not nom_col or not mun_col:
                    continue
                geom_col = _safe_ident(geom_col_raw)
                nom_id = _safe_ident(nom_col)
                mun_id = _safe_ident(mun_col)
                tipo_sel = (
                    f'UPPER(TRIM(BOTH FROM g."{_safe_ident(tipovial_col)}"::text))'
                    if tipovial_col
                    else "NULL::text"
                )

                if cve_col:
                    cve_sel = f'TRIM(BOTH FROM g."{_safe_ident(cve_col)}"::text)'
                elif cvegeo_col:
                    # Atlas c_e: últimos 5 dígitos de cvegeo ≈ cvevial (00016)
                    cg = _safe_ident(cvegeo_col)
                    cve_sel = (
                        f"RIGHT(regexp_replace(TRIM(BOTH FROM g.\"{cg}\"::text), "
                        f"'[^0-9]', '', 'g'), 5)"
                    )
                else:
                    cve_sel = "NULL::text"

                where = [
                    f'g."{geom_col}" IS NOT NULL',
                    f"NULLIF(TRIM(BOTH FROM g.\"{nom_id}\"::text), '') IS NOT NULL",
                    f'TRIM(BOTH FROM g."{mun_id}"::text) = %(mun)s',
                ]
                params: dict[str, Any] = {
                    "mun": mun,
                    "loc": loc,
                    "srid": srid,
                    # Traer tramos; luego se colapsan a 1 etiqueta por cvevial/nombre.
                    # Tope alto: Chilpancingo tiene miles de tramos (antes 2500 cortaba calles).
                    "lim": max(1, min(max(int(limit) * 20, 2000), 100000)),
                }
                join_sql = ""
                if loc_col:
                    where.append(
                        f'TRIM(BOTH FROM g."{_safe_ident(loc_col)}"::text) = %(loc)s'
                    )
                else:
                    join_sql = """
                      JOIN marco.l locpoly
                        ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(mun)s
                       AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
                       AND locpoly.the_geom IS NOT NULL
                    """
                    where.append(
                        f"""ST_Intersects(
                          CASE WHEN ST_SRID(g."{geom_col}")=0 THEN ST_SetSRID(g."{geom_col}",{default_src})
                               ELSE g."{geom_col}" END,
                          ST_Buffer(
                            CASE WHEN ST_SRID(locpoly.the_geom)=0 THEN ST_SetSRID(locpoly.the_geom,3857)
                                 ELSE locpoly.the_geom END,
                            40.0
                          )
                        )"""
                    )

                geom_expr = _force_src_sql(geom_col, default_src)
                sql = f"""
                  SELECT
                    UPPER(TRIM(BOTH FROM g."{nom_id}"::text)) AS nom,
                    {tipo_sel} AS tipo,
                    {cve_sel} AS cve,
                    ST_AsGeoJSON(ST_Transform({geom_expr}, %(srid)s)) AS geojson
                  FROM marco."{_safe_ident(table_name)}" g
                  {join_sql}
                  WHERE {" AND ".join(where)}
                  ORDER BY
                    CASE WHEN UPPER(TRIM(BOTH FROM g."{nom_id}"::text)) IN ('NINGUNO','')
                         THEN 1 ELSE 0 END ASC,
                    LENGTH(TRIM(BOTH FROM g."{nom_id}"::text)) ASC
                  LIMIT %(lim)s
                """
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall() or []
        except Exception:
            log.exception("fetch_vialidad_labels marco.%s falló", table_name)
            continue

        if not rows:
            continue

        # Agrupar tramos de la misma vialidad → 1 etiqueta al centro, con ángulo
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            nom = str(row.get("nom") or "").strip() or "NINGUNO"
            tipo = str(row.get("tipo") or "").strip()
            if tipo.upper() in ("", "NONE", "NULL", "NINGUNO"):
                tipo = ""
            cve = str(row.get("cve") or "").strip()
            if cve:
                digits = "".join(ch for ch in cve if ch.isdigit())
                if digits:
                    cve = digits.zfill(5)[-5:]
            # Clave: cvevial si existe (INEGI); si no, nombre
            key = f"CVE:{cve}" if cve else f"NOM:{nom.upper()}"
            geom = _parse_geojson(row.get("geojson"))
            if geom is None:
                continue
            slot = groups.get(key)
            if slot is None:
                groups[key] = {"nom": nom, "tipo": tipo, "cve": cve, "geoms": [geom]}
            else:
                slot["geoms"].append(geom)
                # Preferir un nombre real sobre NINGUNO
                if nom.upper() != "NINGUNO" and (
                    not slot["nom"] or str(slot["nom"]).upper() == "NINGUNO"
                ):
                    slot["nom"] = nom
                if tipo and not slot.get("tipo"):
                    slot["tipo"] = tipo

        batch: list[dict[str, Any]] = []
        for key, slot in groups.items():
            merged = _merge_line_geoms(list(slot["geoms"]))
            if merged is None:
                continue
            mid, angle = _line_midpoint_angle(merged)
            if mid is None:
                continue
            nom = str(slot["nom"] or "NINGUNO")
            tipo = str(slot.get("tipo") or "").strip()
            cve = str(slot["cve"] or "")
            if mode == "tipo_nom":
                text = format_vialidad_tipo_nom(nomvial=nom, tipovial=tipo)
                if not text:
                    continue
            else:
                # PLR / overview rural: nomvial (cvevial)
                text = f"{nom} ({cve})" if cve else nom
            batch.append(
                {
                    "text": text,
                    "geometry": mid,
                    "layer_id": "ejes",
                    "color": "#1a1a1a",
                    "bold": True,
                    "size": label_size,
                    "style": "along",
                    "anchor": "center",
                    "angle": float(angle),
                    "offset": 0.0,
                    "_key": key,
                    "_line": merged,  # para reubicar etiqueta por tile (multipágina)
                    "nomvial": nom,
                    "tipovial": tipo,
                    "cvevial": cve,
                }
            )

        if not batch:
            continue

        # Si tras transform no caen cerca de manzanas, descartar tabla (CRS malo)
        if not _vialidad_batch_overlaps_manzanas(mun, loc, batch, srid):
            log.warning(
                "vialidad labels marco.%s fuera de manzanas (src=%s) → probar otra tabla",
                table_name,
                default_src,
            )
            continue

        # Respetar tope de etiquetas finales (ya colapsadas). Sin tope artificial bajo.
        batch.sort(
            key=lambda b: (
                1 if "NINGUNO" in str(b.get("text") or "").upper() else 0,
                len(str(b.get("text") or "")),
            )
        )
        batch = batch[: max(1, min(int(limit), 20000))]

        for item in batch:
            key = item.pop("_key", item["text"])
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        if out:
            log.info(
                "vialidad labels: %s desde marco.%s (loc %s-%s, tramos→1/vialidad)",
                len(out),
                table_name,
                mun,
                loc,
            )
            break
    if not out:
        log.warning(
            "vialidad labels: 0 filas para %s-%s (probar marco.e / marco.ea)",
            mun,
            loc,
        )
    return out


def _vialidad_batch_overlaps_manzanas(
    mun: str,
    loc: str,
    batch: list[dict[str, Any]],
    map_srid: int,
) -> bool:
    """True si el bbox de etiquetas solapa el de manzanas (mismo CRS mapa)."""
    xs = [float(b["geometry"].x) for b in batch if b.get("geometry") is not None]
    if not xs:
        return False
    try:
        with _db_cm(True) as conn:
            src_m = _carto_default_src(conn, "m", cve_mun=mun, cve_loc=loc)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ST_XMin(ST_Extent(ST_Transform(
                             ST_SetSRID(the_geom, %(src)s), %(map)s))) AS xmin,
                           ST_XMax(ST_Extent(ST_Transform(
                             ST_SetSRID(the_geom, %(src)s), %(map)s))) AS xmax
                      FROM marco.m
                     WHERE TRIM(cve_mun::text)=%(m)s AND TRIM(cve_loc::text)=%(l)s
                    """,
                    {"src": src_m, "map": map_srid, "m": mun, "l": loc},
                )
                r = cur.fetchone() or {}
        xmin, xmax = r.get("xmin"), r.get("xmax")
        if xmin is None or xmax is None:
            return True  # sin manzanas: no descartar
        return not (max(xs) < float(xmin) or min(xs) > float(xmax))
    except Exception:
        log.exception("overlap vialidad/manzanas falló")
        return True


def fetch_template_labels(
    layer_defs: list[LayerDef],
    *,
    cve_mun: Optional[str] = None,
    cve_loc: Optional[str] = None,
    bbox: Optional[tuple[float, float, float, float]] = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for layer in layer_defs:
        if layer.id == "ejes":
            # Siempre el fetch dedicado (no depende de ST_Azimuth / along frágil)
            eje_size = float(getattr(layer, "label_size", 0) or 0)
            eje_fmt = str(getattr(layer, "label_format", "") or "").strip().lower()
            eje_mode = "tipo_nom" if eje_fmt in ("tipo_nom", "tiponom") else "paren"
            labs = fetch_vialidad_labels(
                cve_mun=cve_mun,
                cve_loc=cve_loc,
                limit=int(getattr(layer, "label_limit", 0) or 300),
                size=eje_size if eje_size > 0 else 4.8,
                text_mode=eje_mode,
            )
            out.extend(labs)
            continue
        if layer.id == "ageb" and layer.label_field:
            labs = fetch_urban_ageb_labels(
                cve_mun=cve_mun,
                cve_loc=cve_loc,
                limit=int(getattr(layer, "label_limit", 0) or 500),
                color=layer.label_color,
                bold=layer.label_bold,
                size=layer.label_size,
            )
            if labs:
                out.extend(labs)
                continue
        if layer.id == "colindantes" and layer.label_field:
            labs = fetch_colindantes_labels(
                cve_mun=cve_mun,
                cve_loc=cve_loc,
                limit=int(getattr(layer, "label_limit", 0) or 120),
                color=layer.label_color,
                bold=layer.label_bold,
                size=layer.label_size,
            )
            if labs:
                out.extend(labs)
                continue
        labs = fetch_layer_labels(
            layer, cve_mun=cve_mun, cve_loc=cve_loc, bbox=bbox
        )
        if (
            not labs
            and layer.table in ("marco.cd", "marco.pe")
            and layer.label_field
            and cve_mun
            and cve_loc
        ):
            labs = fetch_labels_near_localidad(
                layer, cve_mun=cve_mun, cve_loc=cve_loc
            )
        out.extend(labs)
    return out


def fetch_labels_near_localidad(
    layer: LayerDef,
    *,
    cve_mun: str,
    cve_loc: str,
) -> list[dict[str, Any]]:
    """Etiquetas CD (cve_mza) por proximidad a marco.l cuando falla el filtro cve_loc."""
    from column_resolver import resolve_column

    mun = _norm_cve3(cve_mun)
    loc = _norm_cve4(cve_loc)
    if not mun or not loc or not layer.label_field:
        return []

    schema, table_name = parse_table_ref(layer.table)
    if schema != "marco":
        return []

    _, srid = _srid_from_settings()
    default_src = 3857
    table_sql = _qualify(schema, table_name)

    try:
        with _db_cm(True) as conn:
            geom_col_raw = resolve_column(
                conn,
                schema,
                table_name,
                (layer.geom_column, "the_geom", "geom", "geometry"),
            )
            label_col_raw = resolve_column(
                conn,
                schema,
                table_name,
                (
                    layer.label_field,
                    "cve_mza",
                    "CVE_MZA",
                    "mza",
                    "MZA",
                    "nomgeo",
                    "nombre",
                ),
            )
            if not geom_col_raw or not label_col_raw:
                return []
            geom_col = _safe_ident(geom_col_raw)
            label_col = _safe_ident(label_col_raw)
            cve_col_raw = resolve_column(conn, schema, table_name, ("cve_mun", "CVE_MUN"))
            cve_filter = ""
            params: dict[str, Any] = {
                "srid": srid,
                "lim": int(layer.label_limit),
                "mun": mun,
                "loc": loc,
            }
            if cve_col_raw:
                cve_filter = f'AND TRIM(BOTH FROM g."{_safe_ident(cve_col_raw)}"::text) = %(mun)s'

            g_raw = f"""CASE WHEN ST_SRID(g."{geom_col}")=0
                              THEN ST_SetSRID(g."{geom_col}",{default_src})
                              ELSE g."{geom_col}" END"""
            l_raw = """CASE WHEN ST_SRID(locpoly.the_geom)=0
                             THEN ST_SetSRID(locpoly.the_geom,3857)
                             ELSE locpoly.the_geom END"""
            g_m = f"ST_Transform({g_raw}, 3857)"
            l_m = f"ST_Transform({l_raw}, 3857)"

            sql = f"""
              SELECT
                TRIM(BOTH FROM g."{label_col}"::text) AS label_text,
                ST_AsGeoJSON(
                  ST_Transform(ST_Centroid({g_raw}), %(srid)s)
                ) AS geojson
              FROM {table_sql} g
              JOIN marco.l locpoly
                ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(mun)s
               AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
               AND locpoly.the_geom IS NOT NULL
              WHERE g."{geom_col}" IS NOT NULL
                AND NULLIF(TRIM(BOTH FROM g."{label_col}"::text), '') IS NOT NULL
                {cve_filter}
                AND ST_DWithin({g_m}, {l_m}, 2500)
              ORDER BY LENGTH(TRIM(BOTH FROM g."{label_col}"::text)) ASC
              LIMIT %(lim)s
            """
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
    except Exception:
        log.exception("fetch_labels_near_localidad %s falló", layer.id)
        return []

    labels: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("label_text") or "").strip()
        if not text:
            continue
        if text.isdigit():
            text = str(int(text))
        geom = _parse_geojson(row.get("geojson"))
        if geom is None:
            continue
        labels.append(
            {
                "text": text,
                "geometry": geom,
                "layer_id": layer.id,
                "color": layer.label_color,
                "bold": layer.label_bold,
                "italic": bool(getattr(layer, "label_italic", False)),
                "size": layer.label_size,
                "style": layer.label_style,
                "anchor": layer.label_anchor or "offset",
            }
        )
    return labels


def fetch_sip_points(
    *,
    cve_mun: str,
    cve_loc: str,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """SIP de una localidad con clasificación por texto de atributos."""
    from column_resolver import resolve_column

    from cartography_engine.symbols.sip_icons import classify_sip_text

    mun = _norm_cve3(cve_mun)
    loc = _norm_cve4(cve_loc)
    if not mun or not loc:
        return []
    _, srid = _srid_from_settings()
    out: list[dict[str, Any]] = []
    try:
        with _db_cm(True) as conn:
            geom_col_raw = resolve_column(
                conn, "marco", "sip", ("the_geom", "geom", "geometry", "wkb_geometry")
            )
            if not geom_col_raw:
                return []
            geom_col = _safe_ident(geom_col_raw)
            # Candidatos típicos INEGI / ogr2ogr
            text_cols = []
            for cand in (
                "geografico",
                "GEOGRÁFICO",
                "geográfic",
                "rasgo",
                "RASGO",
                "tipo",
                "TIPO",
                "nomserv",
                "NOMSERV",
                "nombre",
                "descrip",
                "descripcion",
                "cve_serv",
                "CVE_SERV",
                "servicio",
            ):
                found = resolve_column(conn, "marco", "sip", (cand,))
                if found and found not in text_cols:
                    text_cols.append(found)

            select_extra = ", ".join(f'g."{_safe_ident(c)}" AS a{i}' for i, c in enumerate(text_cols[:6]))
            if select_extra:
                select_extra = ", " + select_extra

            sql = f"""
              SELECT
                ST_AsGeoJSON(
                  ST_Transform(
                    CASE WHEN ST_SRID(g."{geom_col}")=0 THEN ST_SetSRID(g."{geom_col}",3857)
                         ELSE g."{geom_col}" END,
                    %(srid)s
                  )
                ) AS geojson
                {select_extra}
              FROM marco.sip g
              JOIN marco.l locpoly
                ON TRIM(BOTH FROM locpoly.cve_mun::text) = %(mun)s
               AND TRIM(BOTH FROM locpoly.cve_loc::text) = %(loc)s
               AND locpoly.the_geom IS NOT NULL
              WHERE g."{geom_col}" IS NOT NULL
                AND TRIM(BOTH FROM COALESCE(g.cve_mun::text, '')) = %(mun)s
                AND (
                  TRIM(BOTH FROM COALESCE(g.cve_loc::text, '')) = %(loc)s
                  OR ST_Intersects(
                    CASE WHEN ST_SRID(g."{geom_col}")=0 THEN ST_SetSRID(g."{geom_col}",3857)
                         ELSE g."{geom_col}" END,
                    locpoly.the_geom
                  )
                )
              LIMIT %(lim)s
            """
            # cve_loc puede no existir: intentar query simple si falla
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, {"srid": srid, "mun": mun, "loc": loc, "lim": int(limit)})
                    rows = cur.fetchall() or []
            except Exception:
                _safe_rollback(conn)
                sql2 = f"""
                  SELECT
                    ST_AsGeoJSON(
                      ST_Transform(
                        CASE WHEN ST_SRID(g."{geom_col}")=0 THEN ST_SetSRID(g."{geom_col}",3857)
                             ELSE g."{geom_col}" END,
                        %(srid)s
                      )
                    ) AS geojson
                    {select_extra}
                  FROM marco.sip g
                  WHERE g."{geom_col}" IS NOT NULL
                    AND TRIM(BOTH FROM COALESCE(g.cve_mun::text,'')) = %(mun)s
                  LIMIT %(lim)s
                """
                with conn.cursor() as cur:
                    cur.execute(sql2, {"srid": srid, "mun": mun, "lim": int(limit)})
                    rows = cur.fetchall() or []

            for row in rows:
                geom = _parse_geojson(row.get("geojson"))
                if geom is None:
                    continue
                texts = [str(row.get(f"a{i}") or "") for i in range(min(6, len(text_cols)))]
                cls = classify_sip_text(*texts)
                out.append({"geometry": geom, "sip_class": cls, "texts": texts})
    except Exception:
        log.exception("fetch_sip_points falló")
        return []
    return out


def try_import_geopandas():
    try:
        import geopandas as gpd  # noqa: F401

        return gpd
    except ImportError:
        return None


def fetch_colindantes_labels(
    *,
    cve_mun: Optional[str] = None,
    cve_loc: Optional[str] = None,
    limit: int = 120,
    color: str = "#C62828",
    bold: bool = True,
    size: float = 5.2,
) -> list[dict[str, Any]]:
    """Óvalos AGEB rurales colindantes (aux → mgn → marco.a otras L)."""
    from column_resolver import resolve_column

    mun = _norm_cve3(cve_mun or "")
    loc = _norm_cve4(cve_loc or "")
    if not mun or not loc:
        return []
    _, map_srid = _srid_from_settings()
    lim = max(1, min(int(limit or 120), 500))
    candidates = (
        ("aux", "colindantes", False),
        ("mgn", "ageb_rurales_a", False),
        ("marco", "a", True),
    )
    rows: list[Any] = []
    for schema, table, exclude_loc in candidates:
        try:
            with _db_cm(True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM information_schema.tables
                         WHERE table_schema=%(s)s AND table_name=%(t)s LIMIT 1
                        """,
                        {"s": schema, "t": table},
                    )
                    if not cur.fetchone():
                        continue
                ageb_col = resolve_column(
                    conn, schema, table, ("cve_ageb", "CVE_AGEB", "ageb", "CVEAGEB")
                )
                geom_col_raw = resolve_column(
                    conn, schema, table, ("the_geom", "geom", "geometry")
                )
                if not ageb_col or not geom_col_raw:
                    continue
                col = _safe_ident(ageb_col)
                geom_col = _safe_ident(geom_col_raw)
                src = 3857
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            f'SELECT ST_SRID("{geom_col}") AS s FROM {schema}.{table} '
                            f'WHERE "{geom_col}" IS NOT NULL LIMIT 1'
                        )
                        r = cur.fetchone() or {}
                        if int(r.get("s") or 0) > 0:
                            src = int(r["s"])
                except Exception:
                    _safe_rollback(conn)
                g_raw = f"""CASE WHEN ST_SRID(g."{geom_col}")=0
                                THEN ST_SetSRID(g."{geom_col}",{src})
                                ELSE g."{geom_col}" END"""
                l_raw = """CASE WHEN ST_SRID(locpoly.the_geom)=0
                                THEN ST_SetSRID(locpoly.the_geom,3857)
                                ELSE locpoly.the_geom END"""
                mun_col = resolve_column(conn, schema, table, ("cve_mun", "CVE_MUN"))
                mun_filter = (
                    f'AND TRIM(BOTH FROM g."{_safe_ident(mun_col)}"::text)=%(mun)s'
                    if mun_col
                    else ""
                )
                extra = (
                    "AND TRIM(BOTH FROM g.cve_loc::text) <> %(loc)s" if exclude_loc else ""
                )
                pt_expr = f"""
                  CASE WHEN ST_Dimension(ST_Collect({g_raw})) >= 2
                       THEN ST_PointOnSurface(ST_UnaryUnion(ST_Collect({g_raw})))
                       ELSE ST_Centroid(ST_Collect({g_raw})) END
                """
                sql = f"""
                  SELECT TRIM(BOTH FROM g."{col}"::text) AS ageb,
                         ST_AsGeoJSON(ST_Transform({pt_expr}, %(srid)s)) AS geojson
                    FROM {schema}.{table} g
                    JOIN marco.l locpoly
                      ON TRIM(locpoly.cve_mun::text)=%(mun)s
                     AND TRIM(locpoly.cve_loc::text)=%(loc)s
                   WHERE g."{geom_col}" IS NOT NULL
                     {mun_filter}
                     {extra}
                     AND NULLIF(TRIM(BOTH FROM g."{col}"::text),'') IS NOT NULL
                     AND ST_DWithin(
                       ST_Transform({g_raw},3857),
                       ST_Transform({l_raw},3857),
                       1800.0
                     )
                   GROUP BY 1
                   ORDER BY 1
                   LIMIT %(lim)s
                """
                with conn.cursor() as cur:
                    cur.execute(
                        sql, {"mun": mun, "loc": loc, "srid": map_srid, "lim": lim}
                    )
                    rows = list(cur.fetchall() or [])
            if rows:
                log.info(
                    "colindantes labels: %s desde %s.%s", len(rows), schema, table
                )
                break
        except Exception:
            log.exception("fetch_colindantes_labels %s.%s", schema, table)
            rows = []
            continue

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        raw = str(row.get("ageb") or "").strip()
        text = format_ageb_clave(raw) or raw
        if not text or text.upper() in seen:
            continue
        seen.add(text.upper())
        geom = _parse_geojson(row.get("geojson"))
        if geom is None or getattr(geom, "is_empty", True):
            continue
        out.append(
            {
                "text": text,
                "geometry": geom,
                "layer_id": "colindantes",
                "color": color or "#C62828",
                "bold": bool(bold),
                "size": float(size or 5.2) or 5.2,
                "style": "ageb_oval",
                "anchor": "center",
                "angle": 0.0,
            }
        )
    return out


def format_ageb_clave(raw: str) -> str:
    """Normaliza CVE_AGEB a formato INEGI de mapa: '1916' → '191-6'."""
    s = str(raw or "").strip().upper()
    if not s or s in ("000-0", "0000", "NONE", "NULL"):
        return ""
    if "-" in s:
        parts = s.split("-", 1)
        left = "".join(ch for ch in parts[0] if ch.isalnum())[:3].zfill(3)
        right = "".join(ch for ch in parts[1] if ch.isalnum())[:1] or "0"
        return f"{left}-{right}"
    alnum = "".join(ch for ch in s if ch.isalnum())
    if len(alnum) >= 4:
        return f"{alnum[:3]}-{alnum[3]}"
    if len(alnum) == 3:
        return f"{alnum}-0"
    return alnum


def fetch_urban_ageb_labels(
    *,
    cve_mun: Optional[str] = None,
    cve_loc: Optional[str] = None,
    limit: int = 500,
    color: str = "#C62828",
    bold: bool = False,
    size: float = 2.4,
) -> list[dict[str, Any]]:
    """
    Una etiqueta óvalo por AGEB urbana.
    Preferencia: marco.a (PointOnSurface / centroide por cve_ageb).
    Fallback: DISTINCT cve_ageb en marco.m (centroide de manzanas del AGEB).
    """
    from column_resolver import resolve_column

    mun = _norm_cve3(cve_mun or "")
    loc = _norm_cve4(cve_loc or "")
    if not mun or not loc:
        return []
    _, srid = _srid_from_settings()
    lim = max(1, min(int(limit or 500), 5000))
    out: list[dict[str, Any]] = []

    def _rows_from(table: str) -> list[dict[str, Any]]:
        try:
            with _db_cm(True) as conn:
                ageb_col = resolve_column(
                    conn, "marco", table, ("cve_ageb", "CVE_AGEB", "ageb", "CVEAGEB")
                )
                if not ageb_col:
                    return []
                col = _safe_ident(ageb_col)
                src = _carto_default_src(conn, table, cve_mun=mun, cve_loc=loc)
                geom_expr = _force_src_sql("the_geom", src)
                # Polígono → PointOnSurface; si no, centroide del collect
                pt_expr = f"""
                  CASE
                    WHEN ST_Dimension(ST_Collect({geom_expr})) >= 2
                      THEN ST_PointOnSurface(ST_UnaryUnion(ST_Collect({geom_expr})))
                    ELSE ST_Centroid(ST_Collect({geom_expr}))
                  END
                """
                sql = f"""
                  SELECT TRIM(BOTH FROM g."{col}"::text) AS ageb,
                         ST_AsGeoJSON(ST_Transform({pt_expr}, %(srid)s)) AS geojson
                    FROM marco.{table} g
                   WHERE TRIM(BOTH FROM g.cve_mun::text) = %(mun)s
                     AND TRIM(BOTH FROM g.cve_loc::text) = %(loc)s
                     AND g.the_geom IS NOT NULL
                     AND NULLIF(TRIM(BOTH FROM g."{col}"::text), '') IS NOT NULL
                   GROUP BY 1
                   ORDER BY 1
                   LIMIT %(lim)s
                """
                with conn.cursor() as cur:
                    cur.execute(sql, {"mun": mun, "loc": loc, "srid": srid, "lim": lim})
                    return list(cur.fetchall() or [])
        except Exception:
            log.exception("fetch_urban_ageb_labels marco.%s falló", table)
            return []

    rows = _rows_from("a")
    if len(rows) < 2:
        # Pocas/nulas en marco.a → manzanas (suele traer todas las claves urbanas)
        rows_m = _rows_from("m")
        if len(rows_m) > len(rows):
            rows = rows_m

    seen: set[str] = set()
    for row in rows:
        raw = str(row.get("ageb") or "").strip()
        text = format_ageb_clave(raw) or raw
        if not text:
            continue
        key = text.upper()
        if key in seen:
            continue
        seen.add(key)
        geom = _parse_geojson(row.get("geojson"))
        if geom is None or getattr(geom, "is_empty", True):
            continue
        out.append(
            {
                "text": text,
                "geometry": geom,
                "layer_id": "ageb",
                "color": color or "#C62828",
                "bold": bool(bold),
                "size": float(size or 2.4) or 2.4,
                "style": "ageb_oval",
                "anchor": "center",
                "angle": 0.0,
            }
        )
    log.info("AGEB labels urbanos %s-%s: %s", mun, loc, len(out))
    return out


def fetch_ageb_clave_for_localidad(
    *,
    cve_mun: str,
    cve_loc: str,
) -> str:
    """
    Clave AGEB de la localidad: primero marco.a; si no hay filas, DISTINCT de marco.m.
    (En PLR rurales a menudo marco.a no trae la localidad y la clave vive en manzanas.)
    """
    from column_resolver import resolve_column

    mun = _norm_cve3(cve_mun)
    loc = _norm_cve4(cve_loc)
    if not mun or not loc:
        return ""

    def _pick(conn, schema: str, table: str) -> str:
        ageb_col = resolve_column(
            conn, schema, table, ("cve_ageb", "CVE_AGEB", "ageb", "CVEAGEB")
        )
        if not ageb_col:
            return ""
        col = _safe_ident(ageb_col)
        sql = f"""
          SELECT TRIM(BOTH FROM g."{col}"::text) AS ageb
            FROM {schema}.{table} g
           WHERE TRIM(BOTH FROM g.cve_mun::text) = %(mun)s
             AND TRIM(BOTH FROM g.cve_loc::text) = %(loc)s
             AND NULLIF(TRIM(BOTH FROM g."{col}"::text), '') IS NOT NULL
           GROUP BY 1
           ORDER BY COUNT(*) DESC, 1 ASC
           LIMIT 1
        """
        with conn.cursor() as cur:
            try:
                cur.execute(sql, {"mun": mun, "loc": loc})
                row = cur.fetchone()
            except Exception:
                return ""
        return format_ageb_clave(str((row or {}).get("ageb") or ""))

    try:
        with _db_cm(True) as conn:
            for schema, table in (("marco", "a"), ("marco", "m")):
                clave = _pick(conn, schema, table)
                if clave:
                    return clave
    except Exception:
        log.exception("fetch_ageb_clave_for_localidad falló")
    return ""


def ageb_label_point(
    pe_geom: Any,
    manzanas_geom: Any = None,
    loc_geom: Any = None,
) -> Any:
    """
    Punto para el óvalo AGEB: hueco del PE fuera del amanzanamiento
    (como el plano INEGI: ovalo en espacio libre, no sobre manzanas).
    """
    from shapely.geometry import Point

    base = pe_geom if pe_geom is not None and not getattr(pe_geom, "is_empty", True) else loc_geom
    if base is None or getattr(base, "is_empty", True):
        return None
    try:
        poly = base
        if poly.geom_type in ("LineString", "MultiLineString"):
            poly = poly.buffer(1.0).convex_hull
        elif poly.geom_type == "MultiPolygon":
            poly = max(list(poly.geoms), key=lambda g: g.area)

        free = poly
        if manzanas_geom is not None and not getattr(manzanas_geom, "is_empty", True):
            try:
                span = max(poly.bounds[2] - poly.bounds[0], poly.bounds[3] - poly.bounds[1], 1.0)
                buf = max(35.0, min(120.0, span * 0.035))
                blocked = manzanas_geom
                if blocked.geom_type in ("LineString", "MultiLineString"):
                    blocked = blocked.buffer(buf)
                else:
                    blocked = blocked.buffer(buf * 0.85)
                cand = poly.difference(blocked)
                if cand is not None and not cand.is_empty:
                    free = cand
            except Exception:
                pass

        # Elegir la pieza libre de mayor área (hueco real del PE)
        parts: list = []
        if free.geom_type == "Polygon":
            parts = [free]
        elif free.geom_type == "MultiPolygon":
            parts = list(free.geoms)
        elif hasattr(free, "geoms"):
            parts = [g for g in free.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        if parts:
            best = max(parts, key=lambda g: g.area if g.geom_type == "Polygon" else sum(p.area for p in g.geoms))
            return best.representative_point()

        pt = poly.representative_point()
        return Point(pt.x, pt.y) if not isinstance(pt, Point) else pt
    except Exception:
        try:
            c = base.centroid
            return Point(c.x, c.y)
        except Exception:
            return None
