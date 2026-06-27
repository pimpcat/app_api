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


def layer_uses_mun_filter(cfg: Dict[str, Any]) -> bool:
    return cfg.get("mun_filter") is not False


def layer_uses_cvegeo_filter(conn, cfg: Dict[str, Any]) -> bool:
    if "mun_filter_cvegeo" in cfg:
        return bool(cfg["mun_filter_cvegeo"])
    if cfg.get("from_sql"):
        return True
    table = cfg.get("table") or ""
    if not table:
        return False
    return resolve_column(conn, SCHEMA, table, ("cvegeo", "CVEGEO")) is not None


def layer_geom_column(conn, cfg: Dict[str, Any]) -> str:
    if cfg.get("geom_column"):
        return str(cfg["geom_column"])
    if cfg.get("from_sql"):
        return "the_geom"
    table = cfg.get("table") or ""
    if not table:
        return "the_geom"
    return (
        resolve_column(conn, SCHEMA, table, ("the_geom", "geom", "wkb_geometry"))
        or "the_geom"
    )


def table_attribute_columns(
    conn,
    table: str,
    geom_col: str = "the_geom",
    exclude: Optional[Sequence[str]] = None,
) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, udt_name FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position
            """,
            (SCHEMA, table),
        )
        skip_udt = {"geometry", "geography", "bytea"}
        geom_key = (geom_col or "the_geom").lower()
        skip_names = {geom_key, "the_geom", "geom", "wkb_geometry"}
        if exclude:
            skip_names.update(str(c).lower() for c in exclude)
        cols: List[str] = []
        for r in cur.fetchall():
            name = (r["column_name"] or "").lower()
            if name and name not in skip_names and r["udt_name"] not in skip_udt:
                cols.append(name)
        return cols


def _normalize_export_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    export = cfg.get("export")
    if isinstance(export, str):
        return {"mode": export.strip().lower() or "all"}
    if isinstance(export, dict):
        return dict(export)

    out: Dict[str, Any] = {"mode": "all"}
    if cfg.get("shp_all_table_columns"):
        out["mode"] = "all"
    elif cfg.get("export_columns") or cfg.get("export_columns_kml"):
        out["mode"] = "columns"
        if cfg.get("export_columns"):
            out["columns"] = list(cfg["export_columns"])
        if cfg.get("export_columns_kml"):
            out["columns_kml"] = list(cfg["export_columns_kml"])
    return out


def _export_column_source_table(cfg: Dict[str, Any]) -> str:
    return str(
        cfg.get("export_table")
        or cfg.get("gid_table")
        or cfg.get("table")
        or ""
    ).strip()


def _pick_explicit_columns(
    conn,
    cfg: Dict[str, Any],
    export: Dict[str, Any],
    fmt: str,
    geom_col: str,
) -> List[str]:
    fmt_key = "columns_kml" if fmt == "kml" else "columns_shp"
    legacy_key = "export_columns_kml" if fmt == "kml" else "export_columns_shp"
    explicit = (
        export.get(fmt_key)
        or export.get("columns")
        or cfg.get(legacy_key)
        or (cfg.get("export_columns") if fmt == "kml" else None)
    )
    if not explicit:
        return []

    source = _export_column_source_table(cfg)
    if not source:
        return [c for c in explicit if c.lower() != geom_col]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s
            """,
            (SCHEMA, source),
        )
        all_cols = {r["column_name"].lower() for r in cur.fetchall()}
    return [
        c for c in explicit
        if c.lower() in all_cols and c.lower() != geom_col
    ]


def _from_sql_subquery_is_full_table(cfg: Dict[str, Any]) -> bool:
    """True si el subquery expone todas las columnas de la tabla (p. ej. DENUE SELECT *)."""
    if cfg.get("export_subquery_full"):
        return True
    sql = (cfg.get("from_sql") or "").upper()
    compact = " ".join(sql.split())
    return "SELECT *" in compact


def layer_attribute_columns(
    conn,
    cfg: Dict[str, Any],
    fmt: str = "kml",
) -> List[str]:
    geom_col = layer_geom_column(conn, cfg).lower()
    export = _normalize_export_cfg(cfg)
    mode = (export.get("mode") or "all").lower()
    exclude = [str(c) for c in (export.get("exclude") or [])]

    if cfg.get("from_sql"):
        picked = _pick_explicit_columns(conn, cfg, export, fmt, geom_col)
        if picked:
            return picked
        source = _export_column_source_table(cfg)
        if source and mode == "all" and _from_sql_subquery_is_full_table(cfg):
            return table_attribute_columns(conn, source, geom_col, exclude=exclude)
        # Subquery acotado (RNC simplificada) u otro preset sin SELECT *
        return ["gid", "cve_mun", "tipo_vial"]

    source = _export_column_source_table(cfg)
    if not source:
        return []

    if mode == "columns":
        picked = _pick_explicit_columns(conn, cfg, export, fmt, geom_col)
        if picked:
            return picked

    return table_attribute_columns(conn, source, geom_col, exclude=exclude)


def _geom_expr(geom_col: str) -> str:
    q = quote_ident(geom_col)
    return f"ST_AsGeoJSON(ST_Force2D(ST_Transform({q}, 4326)), 6)::text AS geom_json"


def build_select_sql(
    cfg: Dict[str, Any],
    cols: Sequence[str],
    with_cvegeo: bool,
    geom_col: str,
    apply_mun_filter: bool = True,
) -> str:
    attr = ", ".join(quote_ident(c) for c in cols)
    geom = _geom_expr(geom_col)
    q_geom = quote_ident(geom_col)
    where_parts = [f"{q_geom} IS NOT NULL"]
    if apply_mun_filter:
        where_parts.append(mun_where_sql("", with_cvegeo))
    where = " AND ".join(where_parts)
    if cfg.get("from_sql"):
        return (
            f"SELECT {attr}, {geom} FROM {cfg['from_sql']}"
            f" WHERE {where} LIMIT {MAX_FEATURES}"
        )
    table = qualified(cfg["table"])
    return (
        f"SELECT {attr}, {geom} FROM {table}"
        f" WHERE {where} LIMIT {MAX_FEATURES}"
    )


def build_count_sql(
    cfg: Dict[str, Any],
    with_cvegeo: bool,
    geom_col: str,
    apply_mun_filter: bool = True,
) -> str:
    where_parts = [f"{quote_ident(geom_col)} IS NOT NULL"]
    if apply_mun_filter:
        where_parts.append(mun_where_sql("", with_cvegeo))
    where = " AND ".join(where_parts)
    from_part = cfg["from_sql"] if cfg.get("from_sql") else qualified(cfg["table"])
    return f"SELECT COUNT(*)::int AS n FROM {from_part} WHERE {where}"


def _coord_pairs(coords: Sequence) -> List[str]:
    pairs: List[str] = []
    for pt in coords:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            pairs.append(f"{float(pt[0])},{float(pt[1])},0")
    return pairs


def _kml_ground_line_extras() -> str:
    return "<tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode>"


def _kml_line_string(pairs: Sequence[str]) -> str:
    if len(pairs) < 2:
        return ""
    return (
        f"<LineString>{_kml_ground_line_extras()}"
        f"<coordinates>{' '.join(pairs)}</coordinates></LineString>"
    )


def flatten_geometries_for_export(geom: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Descompone geometrías compuestas (como el export SHP: un trazo por parte)."""
    gtype = geom.get("type")
    if gtype == "GeometryCollection":
        out: List[Dict[str, Any]] = []
        for part in geom.get("geometries") or []:
            if isinstance(part, dict):
                out.extend(flatten_geometries_for_export(part))
        return out
    return normalize_geometries_for_shp(geom)


def geojson_to_kml_fragment(geom: Dict[str, Any]) -> str:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point" and coords:
        return (
            "<Point><altitudeMode>clampToGround</altitudeMode>"
            f"<coordinates>{float(coords[0])},{float(coords[1])},0</coordinates></Point>"
        )
    if gtype == "LineString" and coords:
        return _kml_line_string(_coord_pairs(coords))
    if gtype == "MultiLineString" and coords:
        parts = []
        for line in coords:
            frag = _kml_line_string(_coord_pairs(line))
            if frag:
                parts.append(frag)
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return f"<MultiGeometry>{''.join(parts)}</MultiGeometry>"
    if gtype == "Polygon" and coords:
        rings = []
        for i, ring in enumerate(coords):
            pairs = _coord_pairs(ring)
            if pairs:
                tag = "outerBoundaryIs" if i == 0 else "innerBoundaryIs"
                rings.append(
                    f"<{tag}><LinearRing><coordinates>{' '.join(pairs)}</coordinates></LinearRing></{tag}>"
                )
        if not rings:
            return ""
        return (
            f"<Polygon><extrude>0</extrude><tessellate>1</tessellate>"
            f"<altitudeMode>clampToGround</altitudeMode>{''.join(rings)}</Polygon>"
        )
    if gtype == "MultiPolygon" and coords:
        polys = []
        for poly in coords:
            if not poly:
                continue
            rings = []
            for i, ring in enumerate(poly):
                pairs = _coord_pairs(ring)
                if pairs:
                    tag = "outerBoundaryIs" if i == 0 else "innerBoundaryIs"
                    rings.append(
                        f"<{tag}><LinearRing><coordinates>{' '.join(pairs)}</coordinates></LinearRing></{tag}>"
                    )
            if rings:
                polys.append(
                    "<Polygon><extrude>0</extrude><tessellate>1</tessellate>"
                    f"<altitudeMode>clampToGround</altitudeMode>{''.join(rings)}</Polygon>"
                )
        if not polys:
            return ""
        if len(polys) == 1:
            return polys[0]
        return f"<MultiGeometry>{''.join(polys)}</MultiGeometry>"
    if gtype == "MultiPoint" and coords:
        pts = []
        for pt in coords:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                pts.append(
                    "<Point><altitudeMode>clampToGround</altitudeMode>"
                    f"<coordinates>{float(pt[0])},{float(pt[1])},0</coordinates></Point>"
                )
        if not pts:
            return ""
        if len(pts) == 1:
            return pts[0]
        return f"<MultiGeometry>{''.join(pts)}</MultiGeometry>"
    return ""


def _pick_placemark_name(row: Dict[str, Any], cols: Sequence[str]) -> str:
    for key in (
        "nom_estab",
        "nom_insti",
        "nom_comer",
        "nom_insadm",
        "nom_tipo",
        "tipo",
        "nom_loc",
        "nomvial",
        "nomvial1",
        "descripcio",
        "gid",
    ):
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
        parts = flatten_geometries_for_export(geom)
        if not parts:
            continue
        base_name = _pick_placemark_name(row, cols)
        desc = _attr_table_html(row, cols)
        for i, part in enumerate(parts):
            kml_geom = geojson_to_kml_fragment(part)
            if not kml_geom:
                continue
            name = base_name if len(parts) == 1 else f"{base_name} ({i + 1})"
            buf.write(b"<Placemark>\n")
            buf.write(f"<name>{escape(name)}</name>\n".encode())
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
    if gtype == "GeometryCollection":
        out: List[Dict[str, Any]] = []
        for part in geom.get("geometries") or []:
            if isinstance(part, dict):
                out.extend(normalize_geometries_for_shp(part))
        return out
    return []


def _geom_family(gtype: Optional[str]) -> str:
    if gtype in ("Point", "MultiPoint"):
        return "point"
    if gtype in ("LineString", "MultiLineString"):
        return "line"
    if gtype in ("Polygon", "MultiPolygon"):
        return "polygon"
    return ""


def _infer_export_geom_type(rows: Sequence[Dict[str, Any]], catalog_type: Optional[str]) -> str:
    counts = {"point": 0, "line": 0, "polygon": 0}
    for row in rows:
        gj = row.get("geom_json")
        if not gj:
            continue
        try:
            geom = json.loads(gj)
        except json.JSONDecodeError:
            continue
        for part in normalize_geometries_for_shp(geom):
            family = _geom_family(part.get("type"))
            if family:
                counts[family] += 1
    if counts["polygon"] > 0:
        return "polygon"
    if counts["line"] > 0:
        return "line"
    if counts["point"] > 0:
        return "point"
    return (catalog_type or "polygon").lower()


def _close_ring(ring: List[List[float]]) -> List[List[float]]:
    if len(ring) >= 3 and ring[0] != ring[-1]:
        return ring + [ring[0]]
    return ring


def _polygon_exterior_lines(coords: Sequence) -> List[List[List[float]]]:
    """Anillos exteriores de polígono como LineString (export SHP tipo línea)."""
    lines: List[List[List[float]]] = []
    if not coords:
        return lines
    exterior = _xy_coords(coords[0] if coords else [])
    if len(exterior) >= 2:
        lines.append(exterior)
    return lines


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
    effective_type = _infer_export_geom_type(rows, geom_type)
    shape_type = _shp_shape_type(effective_type)
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
                    if effective_type == "line":
                        for line in _polygon_exterior_lines(coords):
                            if len(line) < 2:
                                continue
                            w.record(*rec)
                            w.line([line])
                            written += 1
                        continue
                    rings = [_close_ring(_xy_coords(ring)) for ring in coords if ring]
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
    layer_key = (layer_id or "").strip().lower()
    cfg = layer_config(layer_key)
    if not cfg:
        raise ValueError("UNKNOWN_LAYER")
    apply_mun_filter = layer_uses_mun_filter(cfg)
    if apply_mun_filter:
        cve = norm_cve_mun(cve)
        if not cve:
            raise ValueError("MISSING_PARAMS")
    else:
        cve = norm_cve_mun(cve) or "estatal"
    if fmt not in ("kml", "shp"):
        raise ValueError("INVALID_FORMAT")

    with conn.cursor() as cur:
        cur.execute("SET statement_timeout TO 120000")

    geom_col = layer_geom_column(conn, cfg)
    cols = layer_attribute_columns(conn, cfg, fmt)
    if not cols:
        raise ValueError("NO_COLUMNS")

    with_cvegeo = layer_uses_cvegeo_filter(conn, cfg) if apply_mun_filter else False
    count_sql = build_count_sql(cfg, with_cvegeo, geom_col, apply_mun_filter)
    sql = build_select_sql(cfg, cols, with_cvegeo, geom_col, apply_mun_filter)
    sql_params = {"cve": cve} if apply_mun_filter else {}

    with conn.cursor() as cur:
        cur.execute(count_sql, sql_params)
        n = int(cur.fetchone()["n"] or 0)
        if n == 0:
            raise ValueError("NO_FEATURES")
        cur.execute(sql, sql_params)
        rows = cur.fetchall()

    slug_layer = re.sub(r"[^a-z0-9]+", "_", layer_key)
    if apply_mun_filter:
        slug_mun = re.sub(r"[^a-z0-9]+", "_", (nom_mun or f"mun_{cve}").lower())
        base = f"atlas_{slug_layer}_{cve}_{slug_mun}"
    else:
        base = f"atlas_{slug_layer}_estatal"

    if fmt == "kml":
        data = stream_kml(rows, cfg.get("label", layer_key), cols, cve, nom_mun or None)
        if b"<Placemark>" not in data:
            raise ValueError("NO_GEOMETRIES")
        return data, f"{base}.kml", "application/vnd.google-earth.kml+xml; charset=UTF-8"

    try:
        data = build_shp_zip(rows, cols, base, cfg.get("geom_type"))
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("SHP export failed for layer=%s cve=%s", layer_key, cve)
        raise ValueError(f"SHP_WRITE_FAILED:{exc}") from exc
    return data, f"{base}_wgs84.zip", "application/zip"
