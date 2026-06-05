"""Exportación KML / Shapefile (ZIP) del visor geográfico."""

import io
import json
import logging
import os
import re
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

import shapefile

from column_resolver import resolve_column
from tables import SCHEMA, qualified
from utils import mun_where_sql, norm_cve_mun, quote_ident
from visor_layers import layer_config

logger = logging.getLogger(__name__)

MAX_FEATURES = 12000
DBF_FIELD_WIDTH = 40
WGS84_PRJ = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)

EXPORT_ERRORS = {
    "UNKNOWN_LAYER": "Capa no exportable.",
    "MISSING_PARAMS": "Usa ?layer=manzanas&format=kml&cve_mun=001",
    "INVALID_FORMAT": "format debe ser kml o shp",
    "NO_COLUMNS": "No se encontraron atributos para la capa.",
    "NO_FEATURES": "No hay elementos en este municipio para esta capa.",
    "NO_GEOMETRIES": "No hay geometrías válidas para exportar.",
    "SHP_WRITE_FAILED": "No se pudo generar el Shapefile.",
}


def export_error_message(code: str) -> str:
    return EXPORT_ERRORS.get(code, code)


def layer_uses_cvegeo_filter(conn, cfg: Dict[str, Any]) -> bool:
    if "mun_filter_cvegeo" in cfg:
        return bool(cfg["mun_filter_cvegeo"])
    if cfg.get("from_sql"):
        return True
    table = cfg.get("table") or ""
    if not table:
        return False
    return resolve_column(conn, SCHEMA, table, ("cvegeo", "CVEGEO")) is not None


def layer_attribute_columns(conn, cfg: Dict[str, Any]) -> List[str]:
    if cfg.get("from_sql"):
        return ["gid", "cve_mun", "tipo_vial"]
    table = cfg.get("table", "")
    if cfg.get("export_columns"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_schema = %s AND table_name = %s
                """,
                (SCHEMA, table),
            )
            all_cols = {r["column_name"].lower() for r in cur.fetchall()}
        return [c for c in cfg["export_columns"] if c.lower() in all_cols and c != "the_geom"]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, udt_name FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position
            """,
            (SCHEMA, table),
        )
        skip = {"geometry", "geography", "bytea"}
        cols = []
        for r in cur.fetchall():
            name = (r["column_name"] or "").lower()
            if name and name != "the_geom" and r["udt_name"] not in skip:
                cols.append(name)
        return cols[:200]


def _geom_expr(from_sql: bool) -> str:
    return "ST_AsGeoJSON(ST_Force2D(ST_Transform(the_geom, 4326)), 6)::text AS geom_json"


def build_select_sql(cfg: Dict[str, Any], cols: Sequence[str], with_cvegeo: bool) -> str:
    attr = ", ".join(quote_ident(c) for c in cols)
    geom = _geom_expr(bool(cfg.get("from_sql")))
    where = mun_where_sql("", with_cvegeo)
    if cfg.get("from_sql"):
        return (
            f"SELECT {attr}, {geom} FROM {cfg['from_sql']}"
            f" WHERE the_geom IS NOT NULL AND {where} LIMIT {MAX_FEATURES}"
        )
    table = qualified(cfg["table"])
    return (
        f"SELECT {attr}, {geom} FROM {table}"
        f" WHERE the_geom IS NOT NULL AND {where} LIMIT {MAX_FEATURES}"
    )


def build_count_sql(cfg: Dict[str, Any], with_cvegeo: bool) -> str:
    where = mun_where_sql("", with_cvegeo)
    from_part = cfg["from_sql"] if cfg.get("from_sql") else qualified(cfg["table"])
    return f"SELECT COUNT(*)::int AS n FROM {from_part} WHERE the_geom IS NOT NULL AND {where}"


def _coord_pairs(coords: Sequence) -> List[str]:
    pairs: List[str] = []
    for pt in coords:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            pairs.append(f"{float(pt[0])},{float(pt[1])},0")
    return pairs


def geojson_to_kml_fragment(geom: Dict[str, Any]) -> str:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point" and coords:
        return f"<Point><coordinates>{float(coords[0])},{float(coords[1])},0</coordinates></Point>"
    if gtype == "LineString" and coords:
        pairs = _coord_pairs(coords)
        if not pairs:
            return ""
        return f"<LineString><coordinates>{' '.join(pairs)}</coordinates></LineString>"
    if gtype == "MultiLineString" and coords:
        parts = []
        for line in coords:
            pairs = _coord_pairs(line)
            if pairs:
                parts.append(f"<LineString><coordinates>{' '.join(pairs)}</coordinates></LineString>")
        if not parts:
            return ""
        return f"<MultiGeometry>{''.join(parts)}</MultiGeometry>"
    if gtype == "Polygon" and coords:
        rings = []
        for ring in coords:
            pairs = _coord_pairs(ring)
            if pairs:
                rings.append(
                    f"<outerBoundaryIs><LinearRing><coordinates>{' '.join(pairs)}</coordinates></LinearRing></outerBoundaryIs>"
                )
        if not rings:
            return ""
        return f"<Polygon>{''.join(rings)}</Polygon>"
    if gtype == "MultiPolygon" and coords:
        polys = []
        for poly in coords:
            if not poly:
                continue
            rings = []
            for ring in poly:
                pairs = _coord_pairs(ring)
                if pairs:
                    tag = "outerBoundaryIs" if not rings else "innerBoundaryIs"
                    rings.append(
                        f"<{tag}><LinearRing><coordinates>{' '.join(pairs)}</coordinates></LinearRing></{tag}>"
                    )
            if rings:
                polys.append(f"<Polygon>{''.join(rings)}</Polygon>")
        if not polys:
            return ""
        if len(polys) == 1:
            return polys[0]
        return f"<MultiGeometry>{''.join(polys)}</MultiGeometry>"
    if gtype == "MultiPoint" and coords:
        pts = []
        for pt in coords:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                pts.append(f"<Point><coordinates>{float(pt[0])},{float(pt[1])},0</coordinates></Point>")
        if not pts:
            return ""
        return f"<MultiGeometry>{''.join(pts)}</MultiGeometry>"
    return ""


def _pick_placemark_name(row: Dict[str, Any], cols: Sequence[str]) -> str:
    for key in ("nom_loc", "nomvial", "nomvial1", "descripcio", "gid"):
        if key in row and row[key] not in (None, ""):
            return str(row[key])
    for c in cols:
        if row.get(c) not in (None, ""):
            return str(row[c])
    return "—"


def _attr_table_html(row: Dict[str, Any], cols: Sequence[str]) -> str:
    rows = "".join(
        f"<tr><th>{escape(str(c))}</th><td>{escape(str(row.get(c, '') or ''))}</td></tr>"
        for c in cols
    )
    return f"<table>{rows}</table>"


def stream_kml(
    rows: Sequence[Dict[str, Any]],
    layer_label: str,
    cols: Sequence[str],
    cve: str,
    nom_mun: Optional[str],
) -> bytes:
    title = f"{layer_label} — Municipio {cve}"
    if nom_mun:
        title += f" ({nom_mun})"
    buf = io.BytesIO()
    buf.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
    buf.write(b'<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n')
    buf.write(f"<name>{escape(title)}</name>\n".encode())
    buf.write("<description>Exportación Atlas Municipal — EPSG:4326 (WGS84)</description>\n".encode("utf-8"))
    for row in rows:
        gj = row.get("geom_json")
        if not gj:
            continue
        try:
            geom = json.loads(gj)
        except json.JSONDecodeError:
            continue
        kml_geom = geojson_to_kml_fragment(geom)
        if not kml_geom:
            continue
        name = escape(_pick_placemark_name(row, cols))
        desc = _attr_table_html(row, cols)
        buf.write(b"<Placemark>\n")
        buf.write(f"<name>{name}</name>\n".encode())
        buf.write(f"<description><![CDATA[{desc}]]></description>\n".encode())
        buf.write(b"<ExtendedData>\n")
        for c in cols:
            val = escape(str(row.get(c, "") or ""))
            buf.write(f'<Data name="{escape(str(c))}"><value>{val}</value></Data>\n'.encode())
        buf.write(b"</ExtendedData>\n")
        buf.write(kml_geom.encode())
        buf.write(b"\n</Placemark>\n")
    buf.write(b"</Document></kml>")
    return buf.getvalue()


def normalize_geometries_for_shp(geom: Dict[str, Any]) -> List[Dict[str, Any]]:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point" and coords and len(coords) >= 2:
        return [{"type": "Point", "coordinates": [float(coords[0]), float(coords[1])]}]
    if gtype == "MultiPoint" and coords:
        return [
            {"type": "Point", "coordinates": [float(pt[0]), float(pt[1])]}
            for pt in coords
            if isinstance(pt, (list, tuple)) and len(pt) >= 2
        ]
    if gtype == "LineString" and coords:
        return [{"type": "LineString", "coordinates": coords}]
    if gtype == "MultiLineString" and coords:
        return [{"type": "LineString", "coordinates": line} for line in coords if line]
    if gtype == "Polygon" and coords:
        return [{"type": "Polygon", "coordinates": coords}]
    if gtype == "MultiPolygon" and coords:
        return [{"type": "Polygon", "coordinates": poly} for poly in coords if poly]
    return []


def _shp_shape_type(geom_type: Optional[str]) -> int:
    mapping = {
        "point": shapefile.POINT,
        "line": shapefile.POLYLINE,
        "polygon": shapefile.POLYGON,
    }
    return mapping.get((geom_type or "").lower(), shapefile.NULL)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _xy_coords(coords: Sequence) -> List[List[float]]:
    out: List[List[float]] = []
    for pt in coords:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        x = _safe_float(pt[0])
        y = _safe_float(pt[1])
        if x is not None and y is not None:
            out.append([x, y])
    return out


def _dbf_value(row: Dict[str, Any], col: str) -> str:
    val = row.get(col, "")
    if val is None:
        return ""
    text = str(val).replace("\0", "").replace("\r", " ").replace("\n", " ")
    return text[:DBF_FIELD_WIDTH]


def _dbf_field_name(name: str, used: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", str(name))[:10] or "fld"
    if not re.match(r"^[A-Za-z]", base):
        base = ("F_" + base)[:10]
    out = base
    n = 1
    while out.upper() in used:
        suffix = str(n)
        out = (base[: 10 - len(suffix)] + suffix)[:10]
        n += 1
    used.add(out.upper())
    return out


def build_shp_zip(
    rows: Sequence[Dict[str, Any]],
    cols: Sequence[str],
    base_name: str,
    geom_type: Optional[str] = None,
) -> bytes:
    shape_type = _shp_shape_type(geom_type)
    safe_base = re.sub(r"[^\w\-.]+", "_", base_name)[:80] or "atlas_export"

    with tempfile.TemporaryDirectory() as tmp:
        shp_base = os.path.join(tmp, safe_base)
        w = shapefile.Writer(shp_base, shapeType=shape_type, encoding="utf-8")
        w.autoBalance = True
        used_names: set = set()
        dbf_fields = [_dbf_field_name(c, used_names) for c in cols]
        for fname in dbf_fields:
            w.field(fname, "C", size=DBF_FIELD_WIDTH)

        written = 0
        for row in rows:
            gj = row.get("geom_json")
            if not gj:
                continue
            try:
                geom = json.loads(gj)
            except json.JSONDecodeError:
                continue
            rec = [_dbf_value(row, c) for c in cols]
            for part in normalize_geometries_for_shp(geom):
                gtype = part.get("type")
                coords = part.get("coordinates")
                if gtype == "Point" and coords:
                    x = _safe_float(coords[0])
                    y = _safe_float(coords[1])
                    if x is None or y is None:
                        continue
                    w.record(*rec)
                    w.point(x, y)
                    written += 1
                elif gtype == "LineString" and coords:
                    line = _xy_coords(coords)
                    if len(line) < 2:
                        continue
                    w.record(*rec)
                    w.line([line])
                    written += 1
                elif gtype == "Polygon" and coords:
                    rings = [_xy_coords(ring) for ring in coords if ring]
                    rings = [ring for ring in rings if len(ring) >= 3]
                    if not rings:
                        continue
                    w.record(*rec)
                    w.poly(rings)
                    written += 1

        w.close()
        if written == 0:
            raise ValueError("NO_GEOMETRIES")

        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
            for ext in (".shp", ".shx", ".dbf"):
                path = shp_base + ext
                with open(path, "rb") as fh:
                    zf.writestr(f"{base_name}{ext}", fh.read())
            zf.writestr(f"{base_name}.prj", WGS84_PRJ)
            zf.writestr(f"{base_name}.cpg", "UTF-8")
        return zbuf.getvalue()


def export_layer(conn, layer_id: str, fmt: str, cve: str, nom_mun: str):
    cfg = layer_config(layer_id)
    if not cfg:
        raise ValueError("UNKNOWN_LAYER")
    cve = norm_cve_mun(cve)
    if not cve:
        raise ValueError("MISSING_PARAMS")
    if fmt not in ("kml", "shp"):
        raise ValueError("INVALID_FORMAT")

    with conn.cursor() as cur:
        cur.execute("SET statement_timeout TO 120000")

    cols = layer_attribute_columns(conn, cfg)
    if not cols:
        raise ValueError("NO_COLUMNS")

    with_cvegeo = layer_uses_cvegeo_filter(conn, cfg)
    count_sql = build_count_sql(cfg, with_cvegeo)
    sql = build_select_sql(cfg, cols, with_cvegeo)

    with conn.cursor() as cur:
        cur.execute(count_sql, {"cve": cve})
        n = int(cur.fetchone()["n"] or 0)
        if n == 0:
            raise ValueError("NO_FEATURES")
        cur.execute(sql, {"cve": cve})
        rows = cur.fetchall()

    slug_layer = re.sub(r"[^a-z0-9]+", "_", layer_id.lower())
    slug_mun = re.sub(r"[^a-z0-9]+", "_", (nom_mun or f"mun_{cve}").lower())
    base = f"atlas_{slug_layer}_{cve}_{slug_mun}"

    if fmt == "kml":
        data = stream_kml(rows, cfg.get("label", layer_id), cols, cve, nom_mun or None)
        if b"<Placemark>" not in data:
            raise ValueError("NO_GEOMETRIES")
        return data, f"{base}.kml", "application/vnd.google-earth.kml+xml; charset=UTF-8"

    try:
        data = build_shp_zip(rows, cols, base, cfg.get("geom_type"))
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("SHP export failed for layer=%s cve=%s", layer_id, cve)
        raise ValueError(f"SHP_WRITE_FAILED:{exc}") from exc
    return data, f"{base}_wgs84.zip", "application/zip"
