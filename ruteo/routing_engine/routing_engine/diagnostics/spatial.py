"""
Medidas espaciales para diagnóstico: SRID del Atlas (p. ej. 3857), sin geography.

- Longitud y distancia en metros vía geometría proyectada (ST_Length / ST_Distance).
- ST_Transform a WGS84 solo para exportación GeoJSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Tuple

from column_resolver import resolve_column
from tables import SCHEMA, T_RNC, T_RNC_ROUTING, qualified
from utils import quote_ident

WGS84 = 4326
DEFAULT_PROJECTED_SRID = 3857


@dataclass(frozen=True)
class SpatialContext:
    """Contexto SRID detectado en c_rnc para SQL de medición y exportación."""

    srid: int
    is_projected: bool
    measure_srid: int = DEFAULT_PROJECTED_SRID
    geojson_srid: int = WGS84

    def length_m_sql(self, geom_expr: str) -> str:
        """Longitud en metros sin ::geography."""
        if self.is_projected:
            return f"ST_Length({geom_expr})"
        return f"ST_Length(ST_Transform({geom_expr}, {self.measure_srid}))"

    def distance_m_sql(self, geom_a: str, geom_b: str) -> str:
        """Distancia en metros entre dos geometrías del mismo espacio de trabajo."""
        if self.is_projected:
            return f"ST_Distance({geom_a}, {geom_b})"
        return (
            f"ST_Distance(ST_Transform({geom_a}, {self.measure_srid}), "
            f"ST_Transform({geom_b}, {self.measure_srid}))"
        )

    def as_geojson_sql(self, geom_expr: str) -> str:
        """GeoJSON en WGS84 para visores web / QGIS."""
        if self.srid == self.geojson_srid:
            return f"ST_AsGeoJSON({geom_expr})"
        return f"ST_AsGeoJSON(ST_Transform({geom_expr}, {self.geojson_srid}))"

    def wgs84_xy_sql(self, geom_expr: str) -> Tuple[str, str]:
        """Expresiones ST_X / ST_Y en grados WGS84."""
        if self.srid == self.geojson_srid:
            return f"ST_X({geom_expr})", f"ST_Y({geom_expr})"
        g = f"ST_Transform({geom_expr}, {self.geojson_srid})"
        return f"ST_X({g})", f"ST_Y({g})"


def _srid_is_projected(conn, srid: int) -> bool:
    """True si el SRID es un sistema proyectado (metros), sin funciones PostGIS opcionales."""
    if srid <= 0:
        return False
    if srid == WGS84:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT srtext FROM spatial_ref_sys WHERE srid = %s LIMIT 1",
            (srid,),
        )
        row = cur.fetchone()
    if not row or not row.get("srtext"):
        # Atlas trabaja en 3857; cualquier SRID distinto de WGS84 se trata como proyectado.
        return True
    srtext = str(row["srtext"]).strip().upper()
    if srtext.startswith("PROJCS"):
        return True
    if srtext.startswith("GEOGCS"):
        return False
    return srid != WGS84


@lru_cache(maxsize=1)
def detect_spatial_context(_cache_key: int = 1) -> SpatialContext:
    """
    Detecta SRID de ``c_rnc.the_geom`` (una vez por proceso).

    Para uso con conexión fresca, llamar ``detect_spatial_context_conn(conn)``.
    """
    from database import connect

    with connect() as conn:
        return detect_spatial_context_conn(conn)


def detect_spatial_context_conn(conn) -> SpatialContext:
    """Detecta SRID a partir de una fila con geometría en c_rnc."""
    geom_col = resolve_column(conn, SCHEMA, T_RNC, ["the_geom", "geom", "wkb_geometry"])
    if not geom_col:
        return SpatialContext(srid=DEFAULT_PROJECTED_SRID, is_projected=True)
    gq = quote_ident(geom_col)
    rnc = qualified(T_RNC)
    sql = f"""
        SELECT ST_SRID({gq})::int AS srid
          FROM {rnc}
         WHERE {gq} IS NOT NULL
           AND ST_SRID({gq}) > 0
         LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    srid = int(row["srid"]) if row and row.get("srid") else DEFAULT_PROJECTED_SRID
    is_projected = _srid_is_projected(conn, srid)
    measure_srid = srid if is_projected else DEFAULT_PROJECTED_SRID
    return SpatialContext(srid=srid, is_projected=is_projected, measure_srid=measure_srid)


def _len_col_ref(conn, table: str, alias: str) -> str | None:
    col = resolve_column(conn, SCHEMA, table, ["longitud_m", "longitud"])
    if not col:
        return None
    return f"{alias}.{quote_ident(col)}"


def length_m_from_rnc_sql(conn, spatial: SpatialContext, geom_expr: str) -> str:
    """Longitud en metros solo desde c_rnc (columna resuelta o ST_Length)."""
    col = _len_col_ref(conn, T_RNC, "c")
    if col:
        return coalesce_length_m_sql(spatial, col, geom_expr)
    return f"{spatial.length_m_sql(geom_expr)}::double precision"


def length_m_from_routing_join_sql(
    conn,
    spatial: SpatialContext,
    geom_expr: str = "c.the_geom",
) -> str:
    """Longitud en metros: routing → c_rnc → ST_Length(geom)."""
    parts: list[str] = []
    r_col = _len_col_ref(conn, T_RNC_ROUTING, "r")
    c_col = _len_col_ref(conn, T_RNC, "c")
    if r_col:
        parts.append(r_col)
    if c_col:
        parts.append(c_col)
    parts.append(spatial.length_m_sql(geom_expr))
    return f"COALESCE({', '.join(parts)})::double precision"


def coalesce_length_m_sql(
    spatial: SpatialContext,
    longitud_col: str | None,
    geom_expr: str,
) -> str:
    """COALESCE(columna longitud, ST_Length proyectado)."""
    if longitud_col:
        return (
            f"COALESCE({longitud_col}, {spatial.length_m_sql(geom_expr)})"
            "::double precision"
        )
    return f"{spatial.length_m_sql(geom_expr)}::double precision"
