"""Renderers vectoriales ReportLab (polígono, línea, punto, texto, norte, escala, leyenda)."""

from __future__ import annotations

import math
from typing import Optional, Sequence, Union, Tuple

from reportlab.lib.colors import Color, HexColor, black, white
from reportlab.pdfgen.canvas import Canvas
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

from cartography_engine.layouts import Box
from cartography_engine.layers import LegendItem
from cartography_engine.symbols import LineSymbol, PointSymbol, PolygonSymbol

AnySymbol = Union[PolygonSymbol, LineSymbol, PointSymbol]


def _hex_color(value: str, alpha: float = 1.0) -> Color:
    base = HexColor(value)
    a = max(0.0, min(1.0, float(alpha)))
    return Color(base.red, base.green, base.blue, alpha=a)


def world_to_page(
    x: float,
    y: float,
    bounds: tuple[float, float, float, float],
    frame: Box,
) -> tuple[float, float]:
    minx, miny, maxx, maxy = bounds
    dx = max(maxx - minx, 1e-9)
    dy = max(maxy - miny, 1e-9)
    scale = min(frame.width / dx, frame.height / dy)
    used_w = dx * scale
    used_h = dy * scale
    ox = frame.x + (frame.width - used_w) / 2.0
    oy = frame.y + (frame.height - used_h) / 2.0
    px = ox + (x - minx) * scale
    py = oy + (y - miny) * scale
    return px, py


def compute_map_scale(
    bounds: tuple[float, float, float, float],
    frame: Box,
) -> float:
    """Escala representativa (1:N) asumiendo bounds en metros."""
    minx, miny, maxx, maxy = bounds
    dx = max(maxx - minx, 1e-9)
    dy = max(maxy - miny, 1e-9)
    scale = min(frame.width / dx, frame.height / dy)
    meters_per_page_point = 0.0254 / 72.0
    if scale <= 0:
        return 0.0
    return (1.0 / scale) / meters_per_page_point


def draw_frame(c: Canvas, box: Box, stroke: float = 1.5) -> None:
    c.setStrokeColor(black)
    c.setLineWidth(stroke)
    c.rect(box.x, box.y, box.width, box.height, stroke=1, fill=0)


def draw_title(c: Canvas, box: Box, text: str) -> None:
    c.setFillColor(black)
    # Escala tipográfica al ancho de página (carta ~14 pt; plotter ~22–26 pt)
    fs = max(13.0, min(26.0, box.width * 0.012))
    c.setFont("Helvetica-Bold", fs)
    c.drawCentredString(box.x + box.width / 2.0, box.y + box.height * 0.38, text[:120])


def draw_footer(c: Canvas, box: Box, text: str) -> None:
    c.setFillColor(HexColor("#444444"))
    fs = max(8.0, min(14.0, box.width * 0.007))
    c.setFont("Helvetica", fs)
    c.drawString(box.x, box.y + box.height * 0.32, text[:180])


def draw_north_arrow(c: Canvas, box: Box) -> None:
    """Norte; la 'N' queda dentro del box (no se solapa con lo de arriba)."""
    cx = box.x + box.width / 2.0
    fs = max(9.0, min(28.0, box.height * 0.26))
    # Reservar franja superior para la N
    n_band = fs * 1.15
    tip_y = box.y2 - n_band - max(2.0, box.height * 0.02)
    base_y = box.y + max(6.0, box.height * 0.18)
    wing = max(8.0, box.width * 0.28)
    c.setFillColor(black)
    path = c.beginPath()
    path.moveTo(cx, tip_y)
    path.lineTo(cx - wing, base_y)
    path.lineTo(cx, base_y + wing * 0.55)
    path.lineTo(cx + wing, base_y)
    path.close()
    c.drawPath(path, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", fs)
    c.drawCentredString(cx, box.y2 - fs * 0.85, "N")


def _nice_scale_length_m(target_m: float) -> float:
    if target_m <= 0:
        return 1000.0
    exp = math.floor(math.log10(target_m))
    base = 10 ** exp
    for mult in (1, 2, 5, 10):
        candidate = mult * base
        if candidate >= target_m * 0.6:
            return float(candidate)
    return float(10 * base)


def draw_scale_bar(
    c: Canvas,
    box: Box,
    map_scale: float,
    *,
    width_ratio: float = 0.78,
    align: str = "left",
) -> None:
    """Barra de escala gráfica. map_scale = denominador (1:N), mundo en metros."""
    fs = max(9.0, min(18.0, box.height * 0.32))
    if map_scale <= 0:
        c.setFont("Helvetica", fs)
        c.setFillColor(black)
        c.drawString(box.x, box.y + box.height * 0.35, "Escala n/d")
        return

    fill = max(0.5, min(1.0, float(width_ratio)))
    target_page_pts = box.width * fill
    meters_per_pt = (map_scale * 0.0254) / 72.0
    target_m = target_page_pts * meters_per_pt
    nice_m = _nice_scale_length_m(target_m)
    bar_pts = nice_m / meters_per_pt if meters_per_pt > 0 else target_page_pts
    # No rebasar el ancho del box
    bar_pts = min(bar_pts, box.width * 0.98)

    y = box.y + box.height * 0.38
    if str(align).lower() == "center":
        x0 = box.x + (box.width - bar_pts) / 2.0
    else:
        x0 = box.x
    tick = max(4.0, fs * 0.45)
    c.setStrokeColor(black)
    c.setFillColor(black)
    c.setLineWidth(max(1.2, fs * 0.12))
    # barra segmentada estilo GroSIG
    segs = 4
    seg_w = bar_pts / segs
    bar_h = max(4.5, fs * 0.42)
    for i in range(segs):
        sx = x0 + i * seg_w
        if i % 2 == 0:
            c.rect(sx, y - bar_h / 2, seg_w, bar_h, stroke=1, fill=1)
        else:
            c.setFillColor(white)
            c.rect(sx, y - bar_h / 2, seg_w, bar_h, stroke=1, fill=1)
            c.setFillColor(black)
    c.line(x0, y - tick, x0, y + tick)
    c.line(x0 + bar_pts, y - tick, x0 + bar_pts, y + tick)

    if nice_m >= 1000:
        label = f"{nice_m / 1000:g} km"
    else:
        label = f"{nice_m:g} m"
    c.setFont("Helvetica-Bold", fs)
    c.drawCentredString(x0 + bar_pts / 2.0, y + bar_h / 2 + fs * 0.35, label)
    c.setFont("Helvetica", fs * 0.92)
    scale_txt = f"1:{int(round(map_scale)):,}".replace(",", " ")
    if str(align).lower() == "center":
        c.drawCentredString(x0 + bar_pts / 2.0, y - bar_h / 2 - fs * 1.15, scale_txt)
    else:
        c.drawString(x0, y - bar_h / 2 - fs * 1.15, scale_txt)


def _rings_to_path(c: Canvas, rings: Sequence[Sequence[tuple[float, float]]], bounds, frame: Box):
    path = c.beginPath()
    for ring in rings:
        if not ring:
            continue
        x0, y0 = world_to_page(ring[0][0], ring[0][1], bounds, frame)
        path.moveTo(x0, y0)
        for x, y in ring[1:]:
            px, py = world_to_page(x, y, bounds, frame)
            path.lineTo(px, py)
        path.close()
    return path


def _polygon_rings(geom: Polygon) -> list[list[tuple[float, float]]]:
    rings: list[list[tuple[float, float]]] = []
    exterior = list(geom.exterior.coords)
    rings.append([(float(x), float(y)) for x, y in exterior])
    for interior in geom.interiors:
        rings.append([(float(x), float(y)) for x, y in interior.coords])
    return rings


def _as_polygon_symbol(symbol: Optional[AnySymbol]) -> PolygonSymbol:
    if isinstance(symbol, PolygonSymbol):
        return symbol
    if isinstance(symbol, LineSymbol):
        return PolygonSymbol(
            fill_color="#FFFFFF",
            fill_opacity=0.0,
            stroke_color=symbol.stroke_color,
            stroke_width=symbol.stroke_width,
            dash=getattr(symbol, "dash", None),
        )
    if isinstance(symbol, PointSymbol):
        return PolygonSymbol(
            fill_color=symbol.fill_color,
            fill_opacity=0.9,
            stroke_color=symbol.stroke_color,
            stroke_width=0.6,
        )
    return PolygonSymbol()


def _page_polyline(coords, bounds, frame: Box) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for x, y in coords:
        px, py = world_to_page(x, y, bounds, frame)
        out.append((px, py))
    return out


def _offset_polyline_page(
    pts: Sequence[tuple[float, float]], dist: float
) -> list[tuple[float, float]]:
    """Offset aproximado en espacio página (paralela a izquierda si dist>0)."""
    n = len(pts)
    if n < 2 or abs(dist) < 1e-9:
        return list(pts)
    out: list[tuple[float, float]] = []
    for i in range(n):
        if i == 0:
            ax, ay = pts[0]
            bx, by = pts[1]
            dx, dy = bx - ax, by - ay
        elif i == n - 1:
            ax, ay = pts[i - 1]
            bx, by = pts[i]
            dx, dy = bx - ax, by - ay
        else:
            ax1, ay1 = pts[i - 1]
            bx1, by1 = pts[i]
            ax2, ay2 = pts[i]
            bx2, by2 = pts[i + 1]
            d1x, d1y = bx1 - ax1, by1 - ay1
            d2x, d2y = bx2 - ax2, by2 - ay2
            l1 = math.hypot(d1x, d1y) or 1.0
            l2 = math.hypot(d2x, d2y) or 1.0
            n1x, n1y = -d1y / l1, d1x / l1
            n2x, n2y = -d2y / l2, d2x / l2
            nx, ny = n1x + n2x, n1y + n2y
            ln = math.hypot(nx, ny)
            if ln < 1e-9:
                nx, ny = n1x, n1y
            else:
                nx, ny = nx / ln, ny / ln
            out.append((pts[i][0] + nx * dist, pts[i][1] + ny * dist))
            continue
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L, dx / L
        out.append((pts[i][0] + nx * dist, pts[i][1] + ny * dist))
    return out


def _stroke_page_polyline(
    c: Canvas,
    pts: Sequence[tuple[float, float]],
    *,
    stroke_color: str,
    stroke_width: float,
    dash: Optional[tuple] = None,
) -> None:
    if len(pts) < 2:
        return
    c.setStrokeColor(_hex_color(stroke_color, 1.0))
    c.setLineWidth(stroke_width)
    if dash:
        c.setDash(list(dash), 0)
    else:
        c.setDash([], 0)
    path = c.beginPath()
    path.moveTo(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        path.lineTo(x, y)
    c.drawPath(path, fill=0, stroke=1)


def _draw_lines(
    c: Canvas,
    geom,
    bounds,
    frame: Box,
    stroke_color: str,
    stroke_width: float,
    dash: Optional[tuple] = None,
    decoration: Optional[str] = None,
) -> None:
    c.setStrokeColor(_hex_color(stroke_color, 1.0))
    c.setLineWidth(stroke_width)
    # ReportLab: setDash([a,b,c,...], phase) — no setDash(a,b,c,d)
    if dash:
        c.setDash(list(dash), 0)
    else:
        c.setDash([], 0)

    # Con dash: fusionar tramos cortos (si no, cada segmento reinicia el patrón
    # y la línea se ve “sólida” / continua). Snap une micro-huecos del shape.
    draw_geom = geom
    if dash and geom is not None and not getattr(geom, "is_empty", True):
        try:
            from shapely.ops import linemerge, snap, unary_union

            gtype = getattr(geom, "geom_type", "")
            raw = geom
            if gtype in ("Polygon", "MultiPolygon"):
                raw = getattr(geom, "boundary", None) or geom
            parts: list = []
            if isinstance(raw, LineString):
                parts = [raw]
            elif hasattr(raw, "geoms"):
                for part in raw.geoms:
                    gt = getattr(part, "geom_type", "")
                    if gt == "LineString":
                        parts.append(part)
                    elif gt == "MultiLineString":
                        parts.extend(
                            [p for p in part.geoms if isinstance(p, LineString)]
                        )
            if parts:
                united = unary_union(parts)
                try:
                    minx, miny, maxx, maxy = bounds
                    span = max(maxx - minx, maxy - miny, 1.0)
                    snap_tol = max(3.0, span * 0.00025)
                except Exception:
                    snap_tol = 5.0
                try:
                    united = snap(united, united, snap_tol)
                except Exception:
                    pass
                merged = linemerge(united)
                draw_geom = merged if merged is not None and not getattr(merged, "is_empty", True) else united
            elif gtype in ("MultiLineString", "GeometryCollection", "MultiPolygon", "Polygon"):
                if gtype in ("Polygon", "MultiPolygon"):
                    boundary = getattr(geom, "boundary", None)
                    draw_geom = linemerge(boundary) if boundary is not None else geom
                else:
                    draw_geom = linemerge(geom)
                if draw_geom is None or getattr(draw_geom, "is_empty", True):
                    draw_geom = geom
        except Exception:
            draw_geom = geom

    if isinstance(draw_geom, LineString):
        lines = [draw_geom]
    elif hasattr(draw_geom, "geoms"):
        lines = []
        for part in draw_geom.geoms:
            gt = getattr(part, "geom_type", "")
            if gt == "LineString":
                lines.append(part)
            elif gt == "MultiLineString":
                lines.extend(list(part.geoms))
            elif hasattr(part, "boundary") and part.boundary is not None:
                b = part.boundary
                if isinstance(b, LineString):
                    lines.append(b)
                elif hasattr(b, "geoms"):
                    lines.extend([p for p in b.geoms if isinstance(p, LineString)])
    else:
        lines = []

    # Dash grueso + round cap rellena los huecos → se ve “continuo”.
    # Butt cap mantiene el gap del patrón visible.
    if dash:
        c.setLineCap(0)
        c.setLineJoin(1)
    else:
        c.setLineCap(0)
        c.setLineJoin(0)

    deco = str(decoration or "").strip().lower()

    def _simplify_dense_coords(coords: list) -> list:
        try:
            from shapely.geometry import LineString as _LS

            minx, miny, maxx, maxy = bounds
            span = max(maxx - minx, maxy - miny, 1.0)
            tol = max(8.0, span * 0.0008)
            simp = _LS(coords).simplify(tol, preserve_topology=False)
            if simp is None or simp.is_empty:
                return coords
            if simp.geom_type == "LineString" and len(simp.coords) >= 2:
                return list(simp.coords)
            if simp.geom_type == "MultiLineString":
                best = max(
                    (g for g in simp.geoms if isinstance(g, _LS)),
                    key=lambda g: g.length,
                    default=None,
                )
                if best is not None and len(best.coords) >= 2:
                    return list(best.coords)
        except Exception:
            pass
        return coords

    # Deco especial: se procesa por tramo (offset / ticks).
    if deco in ("double_dash", "double_mixed", "mixed_double", "half_dash"):
        for line in lines:
            coords = _simplify_dense_coords(list(line.coords))
            if len(coords) < 2:
                continue
            page_pts = _page_polyline(coords, bounds, frame)
            sep = max(2.2, stroke_width * 2.6)
            solid_pts = _offset_polyline_page(page_pts, sep * 0.5)
            dash_pts = _offset_polyline_page(page_pts, -sep * 0.5)
            dash_pat = tuple(dash) if dash else (5.0, 3.0)
            lw = max(0.32, min(0.55, stroke_width * 0.55))
            c.setLineCap(1)
            c.setLineJoin(1)
            _stroke_page_polyline(
                c,
                solid_pts,
                stroke_color=stroke_color,
                stroke_width=lw,
                dash=None,
            )
            _stroke_page_polyline(
                c,
                dash_pts,
                stroke_color=stroke_color,
                stroke_width=lw,
                dash=dash_pat,
            )
            c.setLineCap(0)
            c.setLineJoin(0)
        c.setDash([], 0)
        return

    # Doble línea: 2 drawPath (borde + núcleo) para TODA la capa.
    if deco == "double":
        polylines: list[list] = []
        for line in lines:
            coords = _simplify_dense_coords(list(line.coords))
            if len(coords) >= 2:
                polylines.append(coords)
        if not polylines:
            c.setDash([], 0)
            return
        c.setStrokeColor(_hex_color(stroke_color, 1.0))
        c.setLineWidth(stroke_width * 2.15)
        c.setDash([], 0)
        c.setLineCap(1)
        c.setLineJoin(1)
        path = c.beginPath()
        for coords in polylines:
            x0, y0 = world_to_page(coords[0][0], coords[0][1], bounds, frame)
            path.moveTo(x0, y0)
            for x, y in coords[1:]:
                px, py = world_to_page(x, y, bounds, frame)
                path.lineTo(px, py)
        c.drawPath(path, fill=0, stroke=1)
        c.setStrokeColor(white)
        c.setLineWidth(max(0.5, stroke_width * 0.82))
        path2 = c.beginPath()
        for coords in polylines:
            x0, y0 = world_to_page(coords[0][0], coords[0][1], bounds, frame)
            path2.moveTo(x0, y0)
            for x, y in coords[1:]:
                px, py = world_to_page(x, y, bounds, frame)
                path2.lineTo(px, py)
        c.drawPath(path2, fill=0, stroke=1)
        c.setStrokeColor(_hex_color(stroke_color, 1.0))
        c.setLineCap(0)
        c.setLineJoin(0)
        c.setDash([], 0)
        return

    # Trazo normal: UN solo path (moveTo/lineTo por tramo) → 1 drawPath.
    # Deco "cross": solo cruces espaciadas (sin línea continua debajo).
    if deco == "cross":
        c.setDash([], 0)
        try:
            frame_diag = math.hypot(float(frame.width), float(frame.height))
        except Exception:
            frame_diag = 2000.0
        # Más separación a plotter grande → menos +++++ sobrepuestos
        step = max(18.0, min(34.0, frame_diag * 0.008))
        tick = max(5.0, stroke_width * 4.8)  # un poco más grande
        c.setStrokeColor(_hex_color(stroke_color, 1.0))
        c.setLineWidth(max(1.85, stroke_width * 1.55))  # “negrita”
        c.setLineCap(1)
        c.setLineJoin(1)
        for line in lines:
            coords = list(line.coords)
            if len(coords) < 2:
                continue
            page_pts = [
                world_to_page(float(x), float(y), bounds, frame) for x, y in coords
            ]
            acc = 0.0
            next_mark = step * 0.5
            for i in range(len(page_pts) - 1):
                ax, ay = page_pts[i]
                bx, by = page_pts[i + 1]
                seg_len = math.hypot(bx - ax, by - ay)
                if seg_len < 1e-3:
                    continue
                while next_mark <= acc + seg_len + 1e-9:
                    t = (next_mark - acc) / seg_len
                    mx = ax + (bx - ax) * t
                    my = ay + (by - ay) * t
                    c.line(mx - tick, my, mx + tick, my)
                    c.line(mx, my - tick, mx, my + tick)
                    next_mark += step
                acc += seg_len
        c.setLineCap(0)
        c.setLineJoin(0)
        c.setDash([], 0)
        return

    if dash:
        c.setDash(list(dash), 0)
    path = c.beginPath()
    n_sub = 0
    plain_coords: list[list] = []
    for line in lines:
        coords = list(line.coords)
        if len(coords) < 2:
            continue
        plain_coords.append(coords)
        x0, y0 = world_to_page(coords[0][0], coords[0][1], bounds, frame)
        path.moveTo(x0, y0)
        for x, y in coords[1:]:
            px, py = world_to_page(x, y, bounds, frame)
            path.lineTo(px, py)
        n_sub += 1
    if n_sub:
        c.drawPath(path, fill=0, stroke=1)

    if deco == "rail" and plain_coords:
        c.setDash([], 0)
        tick = max(1.8, stroke_width * 2.2)
        step = 8.0
        for coords in plain_coords:
            for i in range(len(coords) - 1):
                ax, ay = world_to_page(coords[i][0], coords[i][1], bounds, frame)
                bx, by = world_to_page(coords[i + 1][0], coords[i + 1][1], bounds, frame)
                seg_len = math.hypot(bx - ax, by - ay)
                if seg_len < 1e-3:
                    continue
                ux, uy = (bx - ax) / seg_len, (by - ay) / seg_len
                nx, ny = -uy, ux
                n_marks = max(1, int(seg_len / step))
                for k in range(n_marks + 1):
                    t = k / max(n_marks, 1)
                    mx, my = ax + ux * seg_len * t, ay + uy * seg_len * t
                    c.line(mx - nx * tick, my - ny * tick, mx + nx * tick, my + ny * tick)
    c.setDash([], 0)


def _draw_triangle_marker(
    c: Canvas,
    px: float,
    py: float,
    size: float,
    stroke: str,
    *,
    line_width: Optional[float] = None,
) -> None:
    """Triángulo hueco (caserío disperso / marco.cd)."""
    s = max(1.6, float(size))
    c.setStrokeColor(_hex_color(stroke, 1.0))
    c.setFillColor(white)
    lw = float(line_width) if line_width is not None else max(0.28, s * 0.07)
    c.setLineWidth(lw)
    c.setDash([], 0)
    path = c.beginPath()
    path.moveTo(px, py + s * 0.95)
    path.lineTo(px - s * 0.95, py - s * 0.75)
    path.lineTo(px + s * 0.95, py - s * 0.75)
    path.close()
    c.drawPath(path, fill=1, stroke=1)


def _density_factor_for_scale(map_scale: Optional[float]) -> float:
    """A escalas urbanas (vista de conjunto) reduce glifos/etiquetas CD."""
    s = float(map_scale or 0.0)
    if s >= 50000:
        return 0.28
    if s >= 35000:
        return 0.36
    if s >= 20000:
        return 0.5
    if s >= 12000:
        return 0.7
    return 1.0


def draw_cd_features_on_map(
    c: Canvas,
    features: Sequence[dict],
    bounds: tuple[float, float, float, float],
    frame: Box,
    *,
    max_points: int = 2000,
    map_scale: Optional[float] = None,
    size_factor: float = 1.0,
) -> None:
    """Triángulo + etiqueta cve_mza en CADA punto (no un solo centroide)."""
    n = 0
    dens = _density_factor_for_scale(map_scale) * max(0.2, float(size_factor or 1.0))
    size = max(1.8, min(8.0, getattr(frame, "width", 800) * 0.0048) * dens)
    fs = max(2.4, min(7.0, size * 0.95))
    tri_lw = max(0.25, size * 0.06)

    def _points_of(geom) -> list[tuple[float, float]]:
        if geom is None or getattr(geom, "is_empty", True):
            return []
        gtype = getattr(geom, "geom_type", "")
        if gtype == "Point":
            return [(float(geom.x), float(geom.y))]
        if gtype == "MultiPoint":
            return [(float(p.x), float(p.y)) for p in geom.geoms]
        if gtype == "GeometryCollection":
            out: list[tuple[float, float]] = []
            for part in geom.geoms:
                out.extend(_points_of(part))
            return out
        try:
            p = geom.representative_point()
            return [(float(p.x), float(p.y))]
        except Exception:
            return []

    for feat in features:
        if n >= max_points:
            break
        geom = feat.get("geometry")
        text = str(feat.get("text") or "").strip()
        if text in ("·", "0", "000", "."):
            text = ""
        for wx, wy in _points_of(geom):
            if n >= max_points:
                break
            px, py = world_to_page(wx, wy, bounds, frame)
            _draw_triangle_marker(c, px, py, size, "#424242", line_width=tri_lw)
            if text:
                c.setFillColor(_hex_color("#212121", 1.0))
                c.setFont("Helvetica-Bold", fs)
                c.drawString(px + size * 0.95, py + size * 0.45, text[:10])
            n += 1


def _draw_plane_marker(c: Canvas, px: float, py: float, size: float, fill: str) -> None:
    """Glifo simple tipo avión (triángulo + cuerpo)."""
    s = max(2.5, float(size))
    c.setFillColor(_hex_color(fill, 1.0))
    c.setStrokeColor(_hex_color(fill, 1.0))
    c.setLineWidth(0.4)
    path = c.beginPath()
    path.moveTo(px, py + s * 1.1)
    path.lineTo(px - s * 0.35, py - s * 0.2)
    path.lineTo(px + s * 0.35, py - s * 0.2)
    path.close()
    c.drawPath(path, fill=1, stroke=0)
    c.setLineWidth(max(0.8, s * 0.22))
    c.line(px - s * 0.95, py + s * 0.15, px + s * 0.95, py + s * 0.15)


def _draw_points(c: Canvas, geom, bounds, frame: Box, symbol: PointSymbol) -> None:
    c.setFillColor(_hex_color(symbol.fill_color, 0.95))
    c.setStrokeColor(_hex_color(symbol.stroke_color, 1.0))
    c.setLineWidth(0.6)
    if isinstance(geom, Point):
        points = [geom]
    elif isinstance(geom, MultiPoint):
        points = list(geom.geoms)
    elif isinstance(geom, GeometryCollection):
        points = [p for p in geom.geoms if isinstance(p, Point)]
        if not points:
            for part in geom.geoms:
                try:
                    rp = part.representative_point()
                    points.append(Point(float(rp.x), float(rp.y)))
                except Exception:
                    continue
    else:
        points = list(getattr(geom, "geoms", []) or [])
    r = max(1.2, float(symbol.size) * 0.55)
    marker = getattr(symbol, "marker", None)
    for pt in points:
        if not isinstance(pt, Point):
            continue
        px, py = world_to_page(float(pt.x), float(pt.y), bounds, frame)
        if marker == "plane":
            _draw_plane_marker(c, px, py, float(symbol.size), symbol.fill_color)
        elif marker in ("triangle", "tri"):
            _draw_triangle_marker(c, px, py, float(symbol.size), symbol.stroke_color)
        else:
            c.circle(px, py, r, fill=1, stroke=1)


def _hatch_polygon(c: Canvas, geom: Polygon, bounds, frame: Box, stroke: str) -> None:
    """Rayado diagonal ligero sobre el bbox del polígono (clip aproximado)."""
    minx, miny, maxx, maxy = geom.bounds
    c.saveState()
    path = _rings_to_path(c, _polygon_rings(geom), bounds, frame)
    c.clipPath(path, stroke=0)
    c.setStrokeColor(_hex_color(stroke, 0.85))
    c.setLineWidth(0.22)
    # Espaciado en mundo → página
    step = max((maxx - minx), (maxy - miny)) / 18.0
    step = max(step, 80.0)
    x = minx - (maxy - miny)
    while x < maxx + (maxy - miny):
        x0, y0 = world_to_page(x, miny, bounds, frame)
        x1, y1 = world_to_page(x + (maxy - miny), maxy, bounds, frame)
        c.line(x0, y0, x1, y1)
        x += step
    c.restoreState()


def _line_symbol_to_boundary(geom: BaseGeometry) -> Optional[BaseGeometry]:
    """Polígono/colección → borde lineal para trazar con LineSymbol (dash inclusive)."""
    if geom is None or geom.is_empty:
        return None
    try:
        if isinstance(geom, (LineString, MultiLineString)):
            return geom
        if isinstance(geom, Polygon):
            return geom.boundary
        if isinstance(geom, MultiPolygon):
            parts = [p.boundary for p in geom.geoms if p is not None and not p.is_empty]
            if not parts:
                return None
            from shapely.ops import unary_union

            return unary_union(parts)
        if isinstance(geom, GeometryCollection):
            parts = []
            for part in geom.geoms:
                b = _line_symbol_to_boundary(part)
                if b is not None and not b.is_empty:
                    parts.append(b)
            if not parts:
                return None
            from shapely.ops import unary_union

            return unary_union(parts)
    except Exception:
        return None
    return None


def draw_geometry(
    c: Canvas,
    geom: BaseGeometry,
    bounds: tuple[float, float, float, float],
    frame: Box,
    symbol: Optional[AnySymbol] = None,
) -> None:
    if geom is None or geom.is_empty:
        return
    # Trazo invisible / desactivado: solo aplica a líneas/puntos.
    # Polígonos con fill deben dibujarse aunque stroke_width sea 0.
    if symbol is not None and not isinstance(symbol, PolygonSymbol):
        try:
            sw = float(getattr(symbol, "stroke_width", 1.0) or 0.0)
            if sw <= 0 and not isinstance(symbol, PointSymbol):
                return
        except (TypeError, ValueError):
            pass

    if isinstance(symbol, PointSymbol) or isinstance(geom, (Point, MultiPoint)):
        pt_sym = symbol if isinstance(symbol, PointSymbol) else PointSymbol()
        if isinstance(geom, (Point, MultiPoint)):
            _draw_points(c, geom, bounds, frame, pt_sym)
            return
        if isinstance(symbol, PointSymbol):
            # Caserío / SIP: polígono → punto representativo
            try:
                pts = []
                if geom.geom_type == "GeometryCollection":
                    for part in geom.geoms:
                        p = part.representative_point()
                        pts.append(Point(p.x, p.y))
                else:
                    p = geom.representative_point()
                    pts.append(Point(p.x, p.y))
                from shapely.geometry import MultiPoint as _MP

                _draw_points(
                    c,
                    pts[0] if len(pts) == 1 else _MP(pts),
                    bounds,
                    frame,
                    pt_sym,
                )
            except Exception:
                pass
            return

    # LineSymbol sobre polígono (p.ej. marco.l): trazar borde CON dash (no relleno sólido)
    if isinstance(symbol, LineSymbol):
        line_geom = geom
        if not isinstance(geom, (LineString, MultiLineString)):
            line_geom = _line_symbol_to_boundary(geom)
        if line_geom is not None and not line_geom.is_empty:
            if isinstance(line_geom, (LineString, MultiLineString)):
                _draw_lines(
                    c,
                    line_geom,
                    bounds,
                    frame,
                    symbol.stroke_color,
                    symbol.stroke_width,
                    getattr(symbol, "dash", None),
                    getattr(symbol, "decoration", None),
                )
                return
            # boundary a veces devuelve GeometryCollection de líneas
            if isinstance(line_geom, GeometryCollection):
                for part in line_geom.geoms:
                    if isinstance(part, (LineString, MultiLineString)):
                        _draw_lines(
                            c,
                            part,
                            bounds,
                            frame,
                            symbol.stroke_color,
                            symbol.stroke_width,
                            getattr(symbol, "dash", None),
                            getattr(symbol, "decoration", None),
                        )
                return

    # Boundaries de AGEB (MultiLineString + PolygonSymbol): solo trazo.
    # NO aplicar a localidades de área (deben rellenarse aunque el clip deje líneas).
    if isinstance(geom, (LineString, MultiLineString)) and isinstance(
        symbol, PolygonSymbol
    ):
        line_sym = LineSymbol(
            stroke_color=symbol.stroke_color,
            stroke_width=symbol.stroke_width,
            dash=getattr(symbol, "dash", None),
        )
        _draw_lines(
            c,
            geom,
            bounds,
            frame,
            line_sym.stroke_color,
            line_sym.stroke_width,
            getattr(line_sym, "dash", None),
            getattr(line_sym, "decoration", None),
        )
        return

    if isinstance(geom, (LineString, MultiLineString)):
        line_sym = symbol if isinstance(symbol, LineSymbol) else LineSymbol()
        _draw_lines(
            c,
            geom,
            bounds,
            frame,
            line_sym.stroke_color,
            line_sym.stroke_width,
            getattr(line_sym, "dash", None),
            getattr(line_sym, "decoration", None),
        )
        return

    sym = _as_polygon_symbol(symbol)
    c.setFillColor(_hex_color(sym.fill_color, sym.fill_opacity))
    c.setStrokeColor(_hex_color(sym.stroke_color, 1.0))
    c.setLineWidth(sym.stroke_width)
    dash = getattr(sym, "dash", None)
    if dash:
        c.setDash(list(dash), 0)
    else:
        c.setDash([], 0)

    if isinstance(geom, Polygon):
        path = _rings_to_path(c, _polygon_rings(geom), bounds, frame)
        c.drawPath(path, fill=1, stroke=1)
        c.setDash([], 0)
        if getattr(sym, "hatch", False):
            _hatch_polygon(c, geom, bounds, frame, sym.stroke_color)
        return

    if isinstance(geom, MultiPolygon):
        # Un path por polígono (no compuesto): even-odd en un solo path
        # anula rellenos cuando hay solapes/anidados (localidades urbanas).
        for part in geom.geoms:
            if part is None or part.is_empty:
                continue
            path = _rings_to_path(c, _polygon_rings(part), bounds, frame)
            c.drawPath(path, fill=1, stroke=1)
            if getattr(sym, "hatch", False):
                _hatch_polygon(c, part, bounds, frame, sym.stroke_color)
        c.setDash([], 0)
        return

    if isinstance(geom, GeometryCollection):
        # Preferir partes poligonales (relleno); no degradar localidades a solo borde.
        poly_parts = [
            p
            for p in geom.geoms
            if isinstance(p, (Polygon, MultiPolygon)) and not p.is_empty
        ]
        if poly_parts and isinstance(symbol, PolygonSymbol):
            for part in poly_parts:
                draw_geometry(c, part, bounds, frame, symbol)
            return
        for part in geom.geoms:
            draw_geometry(c, part, bounds, frame, symbol)
        return

    if isinstance(geom, (LineString, MultiLineString)):
        _draw_lines(
            c,
            geom,
            bounds,
            frame,
            sym.stroke_color,
            sym.stroke_width,
            getattr(sym, "dash", None),
        )
        return

    if isinstance(geom, Point):
        px, py = world_to_page(float(geom.x), float(geom.y), bounds, frame)
        c.circle(px, py, 3, fill=1, stroke=1)
        return


def draw_legend(
    c: Canvas,
    box: Box,
    items: Sequence[LegendItem],
) -> None:
    if not items:
        return

    # Tipografía / swatches escalados al ancho de la columna (plotter ~200 pt)
    k = max(1.0, min(2.4, box.width / 118.0))
    title_fs = round(10.0 * k, 1)
    item_fs = round(9.0 * k, 1)
    row_h = max(18.0, 15.0 * k)
    sw_w = max(22.0, 18.0 * k)
    sw_h = max(10.0, 8.0 * k)
    pad = max(8.0, 6.0 * k)

    c.setFillColor(white)
    c.setStrokeColor(HexColor("#555555"))
    c.setLineWidth(max(0.9, 0.7 * k))
    c.rect(box.x, box.y, box.width, box.height, stroke=1, fill=1)

    # Cabecera de panel
    header_h = title_fs * 2.2
    c.setFillColor(HexColor("#F4F7FA"))
    c.rect(box.x, box.y2 - header_h, box.width, header_h, stroke=0, fill=1)
    c.setStrokeColor(HexColor("#555555"))
    c.line(box.x, box.y2 - header_h, box.x2, box.y2 - header_h)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", title_fs)
    c.drawString(box.x + pad, box.y2 - header_h * 0.65, "Leyenda")

    y = box.y2 - header_h - row_h * 0.85
    for item in items:
        if y < box.y + pad + 10:
            break
        swatch_x = box.x + pad
        swatch_y = y - sw_h * 0.15
        if item.kind == "line":
            stroke = getattr(item.symbol, "stroke_color", "#333333")
            width = float(getattr(item.symbol, "stroke_width", 1.2))
            dash = getattr(item.symbol, "dash", None)
            c.setStrokeColor(_hex_color(str(stroke), 1.0))
            c.setLineWidth(max(1.4, width * k * 0.85))
            if dash:
                c.setDash(list(dash), 0)
            else:
                c.setDash([], 0)
            mid = swatch_y + sw_h * 0.45
            c.line(swatch_x, mid, swatch_x + sw_w, mid)
            c.setDash([], 0)
        elif item.kind == "point":
            fill = getattr(item.symbol, "fill_color", "#C0392B")
            stroke = getattr(item.symbol, "stroke_color", "#FFFFFF")
            c.setFillColor(_hex_color(str(fill), 0.95))
            c.setStrokeColor(_hex_color(str(stroke), 1.0))
            c.setLineWidth(0.7)
            c.circle(swatch_x + sw_w / 2, swatch_y + sw_h * 0.45, max(3.8, 3.2 * k), fill=1, stroke=1)
        else:
            fill = getattr(item.symbol, "fill_color", "#D9E8F5")
            opacity = float(getattr(item.symbol, "fill_opacity", 0.55))
            stroke = getattr(item.symbol, "stroke_color", "#1F4E79")
            c.setFillColor(_hex_color(str(fill), opacity))
            c.setStrokeColor(_hex_color(str(stroke), 1.0))
            c.setLineWidth(max(0.9, 0.8 * k))
            c.rect(swatch_x, swatch_y, sw_w, sw_h, stroke=1, fill=1)

        c.setFillColor(black)
        c.setFont("Helvetica", item_fs)
        label = str(item.label)[:32]
        c.drawString(swatch_x + sw_w + 8, swatch_y + sw_h * 0.15, label)
        y -= row_h


def draw_demo_polygon(c: Canvas, frame: Box) -> None:
    """Geometría sintética para plantilla demo_blank (sin PostGIS)."""
    pad = 24.0
    box = Box(frame.x + pad, frame.y + pad, frame.width - 2 * pad, frame.height - 2 * pad)
    pts = [
        (box.x + box.width * 0.15, box.y + box.height * 0.20),
        (box.x + box.width * 0.45, box.y + box.height * 0.10),
        (box.x + box.width * 0.85, box.y + box.height * 0.25),
        (box.x + box.width * 0.90, box.y + box.height * 0.70),
        (box.x + box.width * 0.55, box.y + box.height * 0.90),
        (box.x + box.width * 0.20, box.y + box.height * 0.75),
    ]
    c.setFillColor(_hex_color("#D9E8F5", 0.6))
    c.setStrokeColor(_hex_color("#1F4E79", 1.0))
    c.setLineWidth(1.5)
    path = c.beginPath()
    path.moveTo(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        path.lineTo(x, y)
    path.close()
    c.drawPath(path, fill=1, stroke=1)

    c.setFillColor(_hex_color("#C0392B", 0.95))
    c.setStrokeColor(white)
    for px, py in (
        (box.x + box.width * 0.35, box.y + box.height * 0.45),
        (box.x + box.width * 0.60, box.y + box.height * 0.55),
        (box.x + box.width * 0.48, box.y + box.height * 0.70),
    ):
        c.circle(px, py, 3.0, fill=1, stroke=1)
