"""Tira inferior GroSIG (plano de localidad) — ReportLab.

Layout: logo CESIEG a todo el alto (izquierda) + columnas sin separadores verticales.
Texto en MAYÚSCULAS salvo nombres de entidad/municipio/localidad (BD).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Optional, Sequence

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from shapely.geometry.base import BaseGeometry

from cartography_engine.branding import get_branding, resolve_logo_paths
from cartography_engine.layouts import Box
from cartography_engine.symbols.legal_texts import ADVERTENCIA_TEXT
from cartography_engine.symbols.sip_icons import SIP_CLASSES, draw_sip_glyph


@dataclass(frozen=True)
class StripContent:
    titulo: str
    entidad: str  # valor BD (no forzar mayúsculas)
    municipio: str
    localidad: str
    escala: float
    armado: str = "1 de 1"
    advertencia: str = ADVERTENCIA_TEXT
    index_mun: Optional[BaseGeometry] = None  # PE (marco.pe)
    index_loc: Optional[BaseGeometry] = None  # mgn.localidades_a
    ageb_clave: str = "000-0"
    manzana_clave: str = "000"
    # Multipágina PLU: bounds de cada carta + índice 0-based de la activa
    index_tiles: Optional[Sequence[tuple[float, float, float, float]]] = None
    active_tile_index: int = 0


@dataclass(frozen=True)
class _StripType:
    title: float
    section: float
    body: float
    small: float
    warn: float
    line_w: float
    sip: float
    row: float
    idx_h: float
    north: float
    bar_h: float


def _strip_type(box: Box) -> _StripType:
    k = max(0.9, min(2.2, box.height / 100.0))
    return _StripType(
        title=round(11.0 * k, 1),
        section=round(6.8 * k, 1),
        body=round(7.4 * k, 1),
        small=round(5.2 * k, 1),
        warn=round(4.8 * k, 1),
        line_w=round(24.0 * max(1.0, k * 0.85), 1),
        sip=round(2.5 * k, 1),
        row=round(7.2 * k, 1),
        idx_h=round(48.0 * max(1.0, k * 0.9), 1),
        north=round(9.5 * k, 1),
        bar_h=round(3.6 * k, 1),
    )


def _up(text: str) -> str:
    return str(text or "").upper()


def _wrap(c: Canvas, text: str, font: str, size: float, max_w: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if c.stringWidth(trial, font, size) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _cesieg_logo_path() -> Optional[Path]:
    for path in resolve_logo_paths():
        if "cesieg" in path.stem.lower():
            return path
    paths = resolve_logo_paths()
    return paths[0] if paths else None


@lru_cache(maxsize=4)
def _cesieg_logo_png_bytes(path_str: str) -> bytes:
    """Fondo negro → transparente + crop al contenido (cacheado)."""
    from PIL import Image

    im = Image.open(path_str).convert("RGBA")
    pixels = list(im.getdata())
    cleaned = []
    for r, g, b, a in pixels:
        if r < 28 and g < 28 and b < 28:
            cleaned.append((0, 0, 0, 0))
        else:
            cleaned.append((r, g, b, a))
    im.putdata(cleaned)
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _cesieg_logo_reader(path: Path) -> ImageReader:
    try:
        return ImageReader(BytesIO(_cesieg_logo_png_bytes(str(path.resolve()))))
    except Exception:
        return ImageReader(str(path))


def _measure_cesieg_logo(usable_h: float, max_w: float) -> tuple[float, Optional[Path]]:
    """Ancho de slot para que el logo ocupe TODO el alto de la tira."""
    path = _cesieg_logo_path()
    if path is None:
        return min(max_w, usable_h * 1.25), None
    try:
        img = _cesieg_logo_reader(path)
        iw, ih = img.getSize()
        scale = usable_h / max(ih, 1)
        need_w = iw * scale
        # Tras el crop el logo es más compacto: dar ancho generoso
        return min(max_w, max(need_w * 1.05, usable_h * 1.1)), path
    except Exception:
        return min(max_w, usable_h * 1.25), path


def _draw_cesieg_logo(c: Canvas, x: float, y: float, w: float, h: float) -> None:
    path = _cesieg_logo_path()
    if path is None:
        branding = get_branding()
        labels = list(branding.get("fallback_labels") or ["CESIEG"])
        label = next((lb for lb in labels if "cesieg" in str(lb).lower()), labels[-1])
        c.setFillColor(HexColor("#6D1B2A"))
        c.setFont("Helvetica-Bold", max(14, min(h * 0.42, w * 0.28)))
        c.drawCentredString(x + w / 2, y + h * 0.42, str(label)[:28].upper())
        return
    try:
        img = _cesieg_logo_reader(path)
        iw, ih = img.getSize()
        # Llenar el alto de la tira
        scale = h / max(ih, 1)
        dw, dh = iw * scale, h
        if dw > w:
            scale = w / max(iw, 1)
            dw, dh = w, ih * scale
        c.drawImage(
            img,
            x + max(0.0, (w - dw) / 2),
            y + max(0.0, (h - dh) / 2),
            width=dw,
            height=dh,
            mask="auto",
        )
    except Exception:
        c.setFillColor(HexColor("#6D1B2A"))
        c.setFont("Helvetica-Bold", max(14, h * 0.35))
        c.drawCentredString(x + w / 2, y + h * 0.42, "CESIEG")


def _geom_to_page_path(
    c: Canvas,
    geom: BaseGeometry,
    bounds: tuple[float, float, float, float],
    frame: Box,
):
    """Proyecta polígonos al minimapa respetando aspect-ratio (como world_to_page)."""
    from cartography_engine.renderers import world_to_page
    from shapely.geometry import MultiPolygon, Polygon

    polys = []
    if isinstance(geom, Polygon):
        polys = [geom]
    elif isinstance(geom, MultiPolygon):
        polys = list(geom.geoms)
    else:
        try:
            polys = [geom.convex_hull]
        except Exception:
            return None
    path = c.beginPath()

    def xy(wx: float, wy: float) -> tuple[float, float]:
        return world_to_page(wx, wy, bounds, frame)

    for poly in polys:
        if poly.is_empty:
            continue
        coords = list(poly.exterior.coords)
        if len(coords) < 3:
            continue
        x0, y0 = xy(coords[0][0], coords[0][1])
        path.moveTo(x0, y0)
        for wx, wy in coords[1:]:
            path.lineTo(*xy(wx, wy))
        path.close()
    return path


def _fit_index_box(
    outer: Box,
    bounds: tuple[float, float, float, float],
) -> Box:
    """Encaja el minimapa al hueco disponible sin estirar la geometría."""
    minx, miny, maxx, maxy = bounds
    bw = max(maxx - minx, 1e-6)
    bh = max(maxy - miny, 1e-6)
    aspect = bw / bh
    if outer.width / max(outer.height, 1e-6) >= aspect:
        idx_h = outer.height
        idx_w = idx_h * aspect
    else:
        idx_w = outer.width
        idx_h = idx_w / aspect
    ox = outer.x + (outer.width - idx_w) / 2.0
    oy = outer.y + (outer.height - idx_h) / 2.0
    return Box(ox, oy, idx_w, idx_h)


def _world_rect_to_page(
    bounds: tuple[float, float, float, float],
    frame: Box,
    rect: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Convierte bounds mundo → rectángulo en página (x, y, w, h)."""
    minx, miny, maxx, maxy = bounds
    dx = max(maxx - minx, 1e-9)
    dy = max(maxy - miny, 1e-9)
    rx0, ry0, rx1, ry1 = rect
    x = frame.x + (rx0 - minx) / dx * frame.width
    y = frame.y + (ry0 - miny) / dy * frame.height
    w = (rx1 - rx0) / dx * frame.width
    h = (ry1 - ry0) / dy * frame.height
    return x, y, w, h


def _draw_index_grid(
    c: Canvas,
    box: Box,
    tiles: Sequence[tuple[float, float, float, float]],
    active_index: int,
    loc: Optional[BaseGeometry],
    *,
    empty_font: float = 7.0,
) -> None:
    """Índice multipágina: grilla de cartas + hatch en hoja activa + silueta L."""
    c.setStrokeColor(black)
    c.setLineWidth(0.8)
    c.setFillColor(white)
    c.rect(box.x, box.y, box.width, box.height, stroke=1, fill=1)
    if not tiles:
        c.setFillColor(HexColor("#666666"))
        c.setFont("Helvetica", empty_font)
        c.drawCentredString(box.x + box.width / 2, box.y + box.height / 2, "ARMADO")
        return

    minx = min(t[0] for t in tiles)
    miny = min(t[1] for t in tiles)
    maxx = max(t[2] for t in tiles)
    maxy = max(t[3] for t in tiles)
    if loc is not None and not getattr(loc, "is_empty", True):
        try:
            lx0, ly0, lx1, ly1 = loc.bounds
            minx, miny = min(minx, lx0), min(miny, ly0)
            maxx, maxy = max(maxx, lx1), max(maxy, ly1)
        except Exception:
            pass
    pad = 3.0
    outer = Box(box.x + pad, box.y + pad, box.width - 2 * pad, box.height - 2 * pad)
    world = (minx, miny, maxx, maxy)
    frame = _fit_index_box(outer, world)

    active = int(active_index) if active_index is not None else -1
    highlight = active >= 0
    if highlight:
        active = max(0, min(active, len(tiles) - 1))
    for i, rect in enumerate(tiles):
        px, py, pw, ph = _world_rect_to_page(world, frame, rect)
        if highlight and i == active:
            c.setFillColor(HexColor("#BDBDBD"))
            c.setStrokeColor(HexColor("#424242"))
            c.setLineWidth(0.9)
            c.rect(px, py, pw, ph, stroke=1, fill=1)
            # Hatch diagonal
            c.setStrokeColor(HexColor("#757575"))
            c.setLineWidth(0.45)
            step = max(2.5, min(pw, ph) * 0.18)
            x0, x1 = px, px + pw
            y0, y1 = py, py + ph
            # Clipping local al tile
            c.saveState()
            p = c.beginPath()
            p.rect(px, py, pw, ph)
            c.clipPath(p, stroke=0, fill=0)
            d = -ph
            while d < pw:
                c.line(x0 + d, y0, x0 + d + ph, y1)
                d += step
            c.restoreState()
        else:
            c.setFillColor(white)
            c.setStrokeColor(HexColor("#616161"))
            c.setLineWidth(0.55)
            c.rect(px, py, pw, ph, stroke=1, fill=1)

    if loc is not None and not getattr(loc, "is_empty", True):
        path2 = _geom_to_page_path(c, loc, world, frame)
        if path2 is not None:
            c.setStrokeColor(black)
            c.setLineWidth(1.15)
            c.drawPath(path2, stroke=1, fill=0)


def draw_assembly_grid_on_map(
    c: Canvas,
    tiles: Sequence[tuple[float, float, float, float]],
    bounds: tuple[float, float, float, float],
    frame: Box,
) -> None:
    """Grilla de armado sobre el map_frame (hoja índice plotter; sin hatch activa)."""
    from cartography_engine.renderers import world_to_page

    if not tiles:
        return
    c.saveState()
    c.setStrokeColor(HexColor("#212121"))
    c.setFillColor(HexColor("#000000"))
    c.setLineWidth(1.4)
    c.setDash([], 0)
    for i, rect in enumerate(tiles):
        x0, y0, x1, y1 = rect
        p0 = world_to_page(x0, y0, bounds, frame)
        p1 = world_to_page(x1, y0, bounds, frame)
        p2 = world_to_page(x1, y1, bounds, frame)
        p3 = world_to_page(x0, y1, bounds, frame)
        path = c.beginPath()
        path.moveTo(p0[0], p0[1])
        path.lineTo(p1[0], p1[1])
        path.lineTo(p2[0], p2[1])
        path.lineTo(p3[0], p3[1])
        path.close()
        c.drawPath(path, stroke=1, fill=0)
        # Número de carta (1-based) al centro
        cx = (p0[0] + p2[0]) / 2.0
        cy = (p0[1] + p2[1]) / 2.0
        fs = max(7.0, min(18.0, min(abs(p1[0] - p0[0]), abs(p3[1] - p0[1])) * 0.22))
        c.setFillColor(HexColor("#212121"))
        c.setFont("Helvetica-Bold", fs)
        c.drawCentredString(cx, cy - fs * 0.35, str(i + 1))
    c.restoreState()


def _draw_index(
    c: Canvas,
    box: Box,
    pe: Optional[BaseGeometry],
    loc: Optional[BaseGeometry],
    *,
    empty_font: float = 7.0,
    tiles: Optional[Sequence[tuple[float, float, float, float]]] = None,
    active_tile_index: int = 0,
) -> None:
    """Índice 1 hoja (PE+L) o grilla multipágina si hay tiles."""
    if tiles:
        _draw_index_grid(
            c,
            box,
            tiles,
            active_tile_index,
            loc,
            empty_font=empty_font,
        )
        return
    # 1 hoja: PE (marco.pe) de fondo + localidad (mgn.localidades_a)
    c.setStrokeColor(black)
    c.setLineWidth(0.8)
    c.setFillColor(white)
    c.rect(box.x, box.y, box.width, box.height, stroke=1, fill=1)
    base = pe if pe is not None and not getattr(pe, "is_empty", True) else loc
    if base is None or getattr(base, "is_empty", True):
        c.setFillColor(HexColor("#666666"))
        c.setFont("Helvetica", empty_font)
        c.drawCentredString(box.x + box.width / 2, box.y + box.height / 2, "UBICACIÓN")
        return
    pad = 3.0
    outer = Box(box.x + pad, box.y + pad, box.width - 2 * pad, box.height - 2 * pad)
    bounds = base.bounds
    frame = _fit_index_box(outer, bounds)
    if pe is not None and not getattr(pe, "is_empty", True):
        path = _geom_to_page_path(c, pe, bounds, frame)
        if path is not None:
            c.setStrokeColor(HexColor("#616161"))
            c.setFillColor(HexColor("#F5F5F5"))
            c.setLineWidth(1.0)
            c.drawPath(path, stroke=1, fill=1)
    if loc is not None and not getattr(loc, "is_empty", True):
        path2 = _geom_to_page_path(c, loc, bounds, frame)
        if path2 is not None:
            c.setStrokeColor(HexColor("#C62828"))
            c.setFillColor(HexColor("#FFCDD2"))
            c.setLineWidth(1.1)
            c.drawPath(path2, stroke=1, fill=1)


def draw_info_strip(
    c: Canvas,
    box: Box,
    content: StripContent,
) -> None:
    t = _strip_type(box)

    c.setStrokeColor(black)
    c.setLineWidth(1.0)
    c.setFillColor(white)
    c.rect(box.x, box.y, box.width, box.height, stroke=1, fill=1)

    pad_x = max(5.0, box.width * 0.004)
    pad_y = max(1.5, box.height * 0.012)
    x0 = box.x + pad_x
    y0 = box.y + pad_y
    usable_w = box.width - 2 * pad_x
    usable_h = box.height - 2 * pad_y

    # Logo CESIEG: alto de tira −4 % (ajuste fino pedido)
    logo_h = (box.height - 3.0) * 0.96
    logo_y = box.y + (box.height - logo_h) / 2.0
    logo_w, _logo_path = _measure_cesieg_logo(logo_h, usable_w * 0.28)
    _draw_cesieg_logo(c, x0, logo_y, logo_w, logo_h)

    rest_x = x0 + logo_w + 10
    rest_w = usable_w - logo_w - 10

    def _vcenter_top(block_h: float) -> float:
        """Y (baseline del título) para un bloque centrado verticalmente."""
        return y0 + (usable_h - block_h) / 2.0 + block_h

    # ── Col 0: título + georreferencia (ancho al contenido + 2 tabuladores) ──
    title = _up(content.titulo)
    title_fs = t.title * 1.15
    body_fs = t.body * 1.12
    rows_meta = [
        ("ENTIDAD:", content.entidad),
        ("MUNICIPIO:", content.municipio),
        ("LOCALIDAD:", content.localidad),
    ]
    # Ancho real del bloque (no reservar 20% vacío)
    title_probe_w = rest_w * 0.28
    title_lines = _wrap(c, title, "Helvetica-Bold", title_fs, title_probe_w)[:2]
    text_w = 0.0
    for line in title_lines:
        text_w = max(text_w, c.stringWidth(line, "Helvetica-Bold", title_fs))
    for label, value in rows_meta:
        text_w = max(
            text_w, c.stringWidth(f"{label} {value}", "Helvetica", body_fs)
        )
    title_col_w = text_w + 6.0
    # Dos tabuladores (~4 em cada uno) entre georreferencia y LÍMITES
    tab_w = body_fs * 4.0
    gap_after_title = tab_w * 2.0
    content_x = rest_x + title_col_w + gap_after_title
    content_w = max(80.0, rest_w - title_col_w - gap_after_title)
    # Empaquetar leyendas/índice/aviso en el resto (sin huecos grandes)
    col_w = [
        title_col_w,
        content_w * 0.17,  # límites
        content_w * 0.12,  # claves
        content_w * 0.18,  # servicios
        content_w * 0.26,  # índice + escala/norte
        content_w * 0.27,  # advertencia
    ]
    xs = [rest_x, content_x]
    for w in col_w[1:-1]:
        xs.append(xs[-1] + w)

    block_h = (
        len(title_lines) * title_fs * 1.08
        + 3
        + len(rows_meta) * body_fs * 1.22
    )
    y_cursor = _vcenter_top(block_h)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", title_fs)
    for line in title_lines:
        y_cursor -= title_fs
        c.drawString(xs[0], y_cursor, line)
        y_cursor -= title_fs * 0.08
    y_cursor -= 3
    for label, value in rows_meta:
        y_cursor -= body_fs
        c.setFont("Helvetica", body_fs)
        c.drawString(xs[0], y_cursor, f"{label} {value}")
        y_cursor -= body_fs * 0.22

    # Hueco tras títulos de leyenda (salto de línea visible)
    title_gap = max(t.row * 1.15, t.section * 1.05)

    # ── Col 1: LÍMITES ──
    lims = [
        ("ESTATAL", "#CC0000", "cross", None),
        ("MUNICIPAL", "#2E7D32", "dash", (6, 2, 1.2, 2)),
        ("AGEB RURAL COLINDANTE", "#757575", "dash", (6, 2, 1.2, 2)),
        ("LOCALIDAD URBANA Y/O RURAL", "#C62828", "dash", (5, 2.8)),
        ("POLÍGONO ENVOLVENTE", "#757575", "solid", None),
        ("AGEB URBANO", "#C62828", "dash", (7, 2.2, 1.5, 2.2)),
        ("MANZANA", "#757575", "solid", None),
        ("CASERÍO DISPERSO", "#757575", "triangle", None),
    ]
    lim_block_h = t.section + title_gap + t.row * len(lims)
    ly = _vcenter_top(lim_block_h) - t.section
    c.setFont("Helvetica-Bold", t.section)
    c.setFillColor(black)
    c.drawString(xs[1], ly, "LÍMITES")
    ly -= title_gap  # salto de línea antes de ESTATAL
    for label, color, kind, dash in lims:
        c.setStrokeColor(HexColor(color))
        c.setFillColor(HexColor(color))
        c.setLineWidth(max(1.0, t.body * 0.15))
        lx0 = xs[1]
        lx1 = xs[1] + t.line_w
        mid_y = ly + t.body * 0.2
        if kind == "triangle":
            cx = lx0 + t.line_w * 0.35
            s = max(2.6, t.body * 0.48)
            path = c.beginPath()
            path.moveTo(cx, mid_y + s * 0.55)
            path.lineTo(cx - s * 0.6, mid_y - s * 0.4)
            path.lineTo(cx + s * 0.6, mid_y - s * 0.4)
            path.close()
            c.setFillColor(white)
            c.setLineWidth(max(0.55, t.body * 0.08))
            c.drawPath(path, fill=0, stroke=1)
        elif kind == "cross":
            tick = max(1.8, t.body * 0.28)
            n = max(4, int(t.line_w / (tick * 2.8)))
            for i in range(n):
                mx = lx0 + (i + 0.5) * (t.line_w / n)
                c.line(mx - tick, mid_y, mx + tick, mid_y)
                c.line(mx, mid_y - tick, mx, mid_y + tick)
        else:
            if dash:
                c.setDash(list(dash), 0)
            else:
                c.setDash([], 0)
            c.line(lx0, mid_y, lx1, mid_y)
            c.setDash([], 0)
        c.setFillColor(black)
        c.setFont("Helvetica", t.small)
        c.drawString(xs[1] + t.line_w + 5, ly, label)
        ly -= t.row

    # ── Col 2: CLAVES (celda centrada; más aire entre elementos) ──
    ageb_txt = _up((content.ageb_clave or "000-0").strip()[:12])
    ageb_short = ageb_txt.split("-")[0].strip() or ageb_txt
    mza_txt = _up((content.manzana_clave or "000").strip()[:8])
    oval_h = t.body * 2.0
    oval_w = min(col_w[2] - 4, t.body * 6.5)
    cx2 = xs[2] + col_w[2] / 2.0
    gap_after_oval = t.row * 0.7
    gap_ageb_mza = t.row * 1.35
    claves_h = (
        t.section
        + title_gap
        + oval_h
        + gap_after_oval
        + t.row * 0.85  # AGEB label
        + t.body * 1.15  # ageb short
        + gap_ageb_mza
        + t.row * 0.85  # MANZANA label
        + t.body * 1.05  # mza
    )
    ky = _vcenter_top(claves_h) - t.section
    c.setFont("Helvetica-Bold", t.section)
    c.setFillColor(black)
    c.drawCentredString(cx2, ky, "CLAVES")
    oval_y = ky - title_gap - oval_h
    oval_x0 = cx2 - oval_w / 2.0
    c.setStrokeColor(HexColor("#C62828"))
    c.setLineWidth(max(1.0, t.body * 0.14))
    c.ellipse(oval_x0, oval_y, oval_x0 + oval_w, oval_y + oval_h, stroke=1, fill=0)
    c.setFillColor(HexColor("#C62828"))
    c.setFont("Helvetica-Bold", t.body)
    c.drawCentredString(cx2, oval_y + oval_h * 0.32, ageb_txt)
    c.setFillColor(black)
    y_lab = oval_y - gap_after_oval
    c.setFont("Helvetica", t.small)
    c.drawCentredString(cx2, y_lab, "AGEB")
    y_ageb = y_lab - t.body * 1.05
    c.setFont("Helvetica-Bold", t.body * 1.1)
    c.drawCentredString(cx2, y_ageb, ageb_short[:8])
    y_mza_lab = y_ageb - gap_ageb_mza
    c.setFont("Helvetica", t.small)
    c.drawCentredString(cx2, y_mza_lab, "MANZANA")
    c.setFont("Helvetica-Bold", t.body)
    c.drawCentredString(cx2, y_mza_lab - t.body * 1.05, mza_txt)

    # ── Col 3: SERVICIOS (salto de línea tras título) ──
    sip_items = [sc for sc in SIP_CLASSES if sc.key != "tanque"][:8]
    sip_h = t.section + title_gap + t.row * 0.9 * len(sip_items)
    sy = _vcenter_top(sip_h) - t.section
    c.setFont("Helvetica-Bold", t.section)
    c.setFillColor(black)
    c.drawString(xs[3], sy, "SERVICIOS")
    sy -= title_gap  # salto antes de la primera simbología
    for cls in sip_items:
        draw_sip_glyph(
            c, xs[3] + t.sip * 1.1, sy + t.sip * 0.15, cls.key, cls.color, size=t.sip * 0.92
        )
        c.setFillColor(black)
        c.setFont("Helvetica", t.small)
        c.drawString(xs[3] + t.sip * 3.0, sy, _up(cls.label)[:24])
        sy -= t.row * 0.9

    # ── Col 4: índice de armado (izq); escala centrada vs ADVERTENCIA ──
    idx_x = xs[4]
    idx_sub_w = col_w[4] * 0.95

    # Bloque índice: título + armado + minimapa
    head_h = t.section + t.body * 1.2 + 3
    map_pad = 2.0
    idx_map_h = max(t.idx_h * 1.35, usable_h - head_h - 4)
    idx_block_h = head_h + idx_map_h
    if idx_block_h > usable_h:
        idx_map_h = max(40.0, usable_h - head_h - 2)
        idx_block_h = head_h + idx_map_h
    idx_top = _vcenter_top(idx_block_h)
    c.setFont("Helvetica-Bold", t.section)
    c.setFillColor(black)
    c.drawString(idx_x, idx_top - t.section, "ÍNDICE DE ARMADO")
    c.setFont("Helvetica-Bold", t.body)
    c.drawString(idx_x, idx_top - t.section - t.body * 1.1, _up(content.armado))
    idx_map_top = idx_top - head_h
    idx_map_bot = idx_map_top - idx_map_h
    idx_geom = content.index_loc or content.index_mun
    fit_bounds = None
    if content.index_tiles:
        txs0 = min(tt[0] for tt in content.index_tiles)
        tys0 = min(tt[1] for tt in content.index_tiles)
        txs1 = max(tt[2] for tt in content.index_tiles)
        tys1 = max(tt[3] for tt in content.index_tiles)
        fit_bounds = (txs0, tys0, txs1, tys1)
        if idx_geom is not None and not getattr(idx_geom, "is_empty", True):
            try:
                lx0, ly0, lx1, ly1 = idx_geom.bounds
                fit_bounds = (
                    min(txs0, lx0),
                    min(tys0, ly0),
                    max(txs1, lx1),
                    max(tys1, ly1),
                )
            except Exception:
                pass
    elif idx_geom is not None and not getattr(idx_geom, "is_empty", True):
        fit_bounds = idx_geom.bounds
    outer_idx = Box(
        idx_x + map_pad,
        idx_map_bot + map_pad,
        max(12.0, idx_sub_w - 2 * map_pad),
        max(12.0, idx_map_h - 2 * map_pad),
    )
    if fit_bounds is not None:
        idx_box = _fit_index_box(outer_idx, fit_bounds)
    else:
        idx_box = outer_idx
    ati = content.active_tile_index
    if ati is None:
        ati = 0
    _draw_index(
        c,
        idx_box,
        content.index_mun,
        content.index_loc,
        empty_font=t.small,
        tiles=content.index_tiles,
        active_tile_index=int(ati),
    )

    # Norte + escala + barra: centro geométrico entre mapa de armado y ADVERTENCIA
    gap_left = idx_box.x + idx_box.width + 6.0
    gap_right = xs[5] - 4.0
    cx_scale = (gap_left + gap_right) / 2.0
    scale_span = max(36.0, gap_right - gap_left)

    north_sz = max(t.north * 1.15, t.body * 1.6)
    bar_w = min(scale_span * 0.72, 100.0 * (t.body / 7.0))
    scale_block_h = north_sz + t.small * 1.5 + t.body * 1.4 + t.bar_h + 10
    sc_top = _vcenter_top(scale_block_h)
    nx = cx_scale
    ny = sc_top - north_sz * 0.55
    path = c.beginPath()
    path.moveTo(nx, ny + north_sz * 0.55)
    path.lineTo(nx - north_sz * 0.38, ny - north_sz * 0.25)
    path.lineTo(nx + north_sz * 0.38, ny - north_sz * 0.25)
    path.close()
    c.setFillColor(black)
    c.drawPath(path, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", t.small)
    c.drawCentredString(cx_scale, ny - north_sz * 0.25 - t.small * 1.15, "N")

    esc = float(content.escala or 0)
    esc_y = ny - north_sz * 0.25 - t.small * 1.15 - t.body * 1.4
    c.setFont("Helvetica-Bold", t.body)
    c.setFillColor(black)
    if esc > 0:
        c.drawCentredString(
            cx_scale,
            esc_y,
            f"ESCALA: 1: {esc:,.0f}".replace(",", " "),
        )
    else:
        c.drawCentredString(cx_scale, esc_y, "ESCALA: —")

    bar_y = esc_y - t.body * 0.55 - t.bar_h
    bar_x = cx_scale - bar_w / 2.0
    c.setStrokeColor(black)
    c.setFillColor(black)
    c.setLineWidth(0.7)
    c.rect(bar_x, bar_y, bar_w, t.bar_h, stroke=1, fill=0)
    seg = bar_w / 3.0
    c.rect(bar_x, bar_y, seg, t.bar_h, stroke=0, fill=1)
    c.rect(bar_x + 2 * seg, bar_y, seg, t.bar_h, stroke=0, fill=1)

    # ── Col 5: ADVERTENCIA (título centrado; cuerpo sin prefijo) ──
    warn_raw = str(content.advertencia or ADVERTENCIA_TEXT).strip()
    up_raw = warn_raw.upper()
    if up_raw.startswith("ADVERTENCIA:"):
        warn_raw = warn_raw.split(":", 1)[1].strip()
    elif up_raw.startswith("ADVERTENCIA "):
        warn_raw = warn_raw[11:].lstrip(": ").strip()
    warn_txt = _up(warn_raw)
    warn_w = col_w[5] - 4
    cx5 = xs[5] + col_w[5] / 2.0
    lines = _wrap(c, warn_txt, "Helvetica", t.warn, warn_w)
    line_step = t.warn * 1.18
    max_lines = max(5, int((usable_h - t.section * 1.4) / line_step))
    show = lines[:max_lines]
    warn_block_h = t.section * 1.25 + title_gap * 0.6 + len(show) * line_step
    ay = _vcenter_top(warn_block_h)
    c.setFont("Helvetica-Bold", t.section)
    c.setFillColor(black)
    c.drawCentredString(cx5, ay - t.section, "ADVERTENCIA")
    ay = ay - t.section - title_gap * 0.6
    c.setFont("Helvetica", t.warn)
    for line in show:
        ay -= line_step
        c.drawString(xs[5], ay, line)


# Compat con imports antiguos
