"""Panel lateral del croquis municipal GroSIG (plotter 90×70).

Independiente de la tira de plano de localidad (`strip.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from cartography_engine.branding import resolve_logo_paths
from cartography_engine.layouts import Box
from cartography_engine.renderers import draw_north_arrow, draw_scale_bar, world_to_page
from cartography_engine.symbols.legal_texts import ADVERTENCIA_TEXT

# Hidro croquis (cian referencia; distinto a planos de localidad)
_HYDRO_LINE = "#00FFFF"
_HYDRO_FILL = "#80FFFF"
_HYDRO_STROKE = "#00FFFF"
_URBAN_FILL = "#FFF59D"


@dataclass(frozen=True)
class CroquisPanelContent:
    titulo: str = "CROQUIS MUNICIPAL CON MARCO GEOESTADÍSTICO"
    entidad: str = ""
    cve_ent: str = "12"
    municipio: str = ""
    cve_mun: str = ""
    escala: float = 0.0
    advertencia: str = ADVERTENCIA_TEXT
    fecha_actualizacion: str = "DICIEMBRE DEL 2024"
    index_geom: Optional[BaseGeometry] = None
    elipsoide: str = "GRS80"
    proyeccion: str = "CÓNICA CONFORME DE LAMBERT"
    datum: str = "ITRF2008 ÉPOCA 2010.0"


def _fs(box: Box) -> dict[str, float]:
    """Tipografía del panel plotter 90×70 (legible a distancia)."""
    k = max(0.95, min(1.25, box.width / 260.0))
    return {
        "title": round(18.5 * k, 1),
        "section": round(14.5 * k, 1),
        "body": round(12.5 * k, 1),
        "small": round(11.0 * k, 1),
        "tiny": round(8.8 * k, 1),
        "id": round(16.0 * k, 1),
        "id_label": round(12.5 * k, 1),
        "pad": max(10.0, 11.0 * k),
        "section_gap": round(7.5 * k, 1),
        "row": round(12.5 * k * 1.62, 1),
    }


def _wrap(c: Canvas, text: str, font: str, size: float, max_w: float) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        trial = f"{cur} {w}"
        if c.stringWidth(trial, font, size) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _draw_justified(
    c: Canvas,
    text: str,
    font: str,
    size: float,
    x: float,
    y: float,
    max_w: float,
    *,
    bottom: float,
    leading: float,
) -> float:
    """Párrafo justificado (última línea a la izquierda)."""
    lines = _wrap(c, text, font, size, max_w)
    c.setFont(font, size)
    c.setFillColor(black)
    for i, line in enumerate(lines):
        baseline = y - size
        if baseline < bottom:
            break
        words = line.split()
        is_last = i == len(lines) - 1
        if len(words) <= 1 or is_last:
            c.drawString(x, baseline, line)
        else:
            total = sum(c.stringWidth(w, font, size) for w in words)
            gaps = len(words) - 1
            space = (max_w - total) / gaps if gaps else 0.0
            cx = x
            for w in words:
                c.drawString(cx, baseline, w)
                cx += c.stringWidth(w, font, size) + space
        y -= leading
    return y


@lru_cache(maxsize=4)
def _croquis_logo_png_bytes(path_str: str) -> bytes:
    """Crop al contenido real: ignora márgenes negros Y blancos.

    La rutina de la tira de localidad solo transparenta negro; si el PNG del
    CESIEG tiene lienzo blanco, el escudo no crecía al “escalar” el archivo.
    """
    from PIL import Image

    im = Image.open(path_str).convert("RGBA")
    cleaned = []
    for r, g, b, a in im.getdata():
        if a < 10:
            cleaned.append((0, 0, 0, 0))
        elif r < 30 and g < 30 and b < 30:
            cleaned.append((0, 0, 0, 0))
        elif r > 248 and g > 248 and b > 248:
            cleaned.append((0, 0, 0, 0))
        else:
            cleaned.append((r, g, b, a))
    im.putdata(cleaned)
    bbox = im.getbbox()
    if bbox:
        # Recargar original y recortar al bbox (conserva colores del logo)
        im0 = Image.open(path_str).convert("RGBA")
        # Aplicar misma máscara de fondo en el crop
        im0 = im0.crop(bbox)
        # Re-aplicar transparencia de márgenes en el recorte
        pix = []
        for r, g, b, a in im0.getdata():
            if a < 10 or (r < 30 and g < 30 and b < 30) or (r > 248 and g > 248 and b > 248):
                pix.append((0, 0, 0, 0))
            else:
                pix.append((r, g, b, a))
        im0.putdata(pix)
        im = im0
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _croquis_logo_reader(path: Path) -> ImageReader:
    try:
        return ImageReader(BytesIO(_croquis_logo_png_bytes(str(path.resolve()))))
    except Exception:
        return ImageReader(str(path))


def _draw_logos(c: Canvas, box: Box, y_top: float, pad: float) -> float:
    """Logo CESIEG propio del croquis (no reutiliza el crop solo-negro de strip)."""
    paths = resolve_logo_paths()
    path = None
    for p in paths:
        if "cesieg" in p.stem.lower():
            path = p
            break
    if path is None and paths:
        path = paths[0]
    if path is None:
        return y_top

    # Bajar claramente del tope de la franja
    top_inset = max(72.0, pad * 5.5)
    y = y_top - top_inset
    side = max(8.0, pad * 0.4)
    max_w = box.width - 2 * side
    # Hasta ~30 % del alto del panel o casi todo el ancho
    max_h = min(380.0, box.height * 0.30, max_w * 1.6)
    target_w = max_w * 0.96
    cx = box.x + box.width / 2.0

    try:
        img = _croquis_logo_reader(path)
        iw, ih = img.getSize()
        if iw <= 0 or ih <= 0:
            return y_top
        scale = target_w / iw
        if ih * scale > max_h:
            scale = max_h / ih
        w, h = iw * scale, ih * scale
        c.drawImage(
            img,
            cx - w / 2.0,
            y - h,
            width=w,
            height=h,
            mask="auto",
            preserveAspectRatio=False,
        )
        return y - h - 28.0
    except Exception:
        return y_top


def _section(
    c: Canvas,
    x: float,
    y: float,
    text: str,
    fs: float,
    *,
    after: float = 0.0,
    center_x: float | None = None,
) -> float:
    """Título de bloque; deja salto de línea antes del contenido siguiente."""
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", fs)
    label = str(text or "").upper()
    if center_x is not None:
        c.drawCentredString(center_x, y - fs, label)
    else:
        c.drawString(x, y - fs, label)
    # Altura del glifo (fs) + ~1 línea en blanco + after opcional.
    return y - fs * 2.25 - max(0.0, after)


def _leaders(
    c: Canvas,
    x: float,
    y: float,
    label: str,
    right_x: float,
    body_fs: float,
    *,
    draw_swatch=None,
    row: float = 0.0,
    swatch_w: float = 28.0,
) -> float:
    c.setFillColor(black)
    c.setFont("Helvetica", body_fs)
    c.drawString(x, y, label)
    lw = c.stringWidth(label, "Helvetica", body_fs)
    dots_x0 = x + lw + 3
    sw = max(20.0, float(swatch_w))
    if draw_swatch is not None:
        draw_swatch(right_x - sw, y - 1)
        dots_x1 = right_x - sw - 4
    else:
        dots_x1 = right_x
    if dots_x1 > dots_x0 + 8:
        c.setStrokeColor(HexColor("#9E9E9E"))
        c.setLineWidth(0.55)
        c.setDash([1.0, 1.4], 0)
        c.line(dots_x0, y + body_fs * 0.25, dots_x1, y + body_fs * 0.25)
        c.setDash([], 0)
    step = row if row > 0 else body_fs * 1.65
    return y - step


def _clave_sample_drawer(c: Canvas, sample: str, color: str, fs: float):
    """Dibuja la muestra de clave a la derecha (para líderes punteados)."""

    def _draw(sx: float, sy: float) -> None:
        c.setFillColor(HexColor(color))
        c.setFont("Helvetica-Bold", fs)
        c.drawRightString(sx + 28, sy + 1, sample)

    return _draw


def _estatal_cross_swatch(c: Canvas, x: float, y: float, color: str = "#CC0000"):
    """Tres cruces + rojas (límite estatal), como en la tira de localidad."""
    c.setStrokeColor(HexColor(color))
    c.setLineWidth(1.55)
    c.setDash([], 0)
    tick = 4.2
    for i in range(3):
        mx = x + 5.0 + i * 10.5
        mid = y + 3.0
        c.line(mx - tick, mid, mx + tick, mid)
        c.line(mx, mid - tick, mx, mid + tick)


def _line_swatch(c: Canvas, x: float, y: float, color: str, width: float, dash=None):
    c.setStrokeColor(HexColor(color))
    c.setLineWidth(width)
    if dash:
        c.setDash(list(dash), 0)
    else:
        c.setDash([], 0)
    c.line(x, y + 3, x + 26, y + 3)
    c.setDash([], 0)


def _mixed_double_swatch(c: Canvas, x: float, y: float, color: str = "#212121"):
    """3866: paralela continua + discontinua (sin casamiento)."""
    c.setStrokeColor(HexColor(color))
    c.setLineWidth(0.55)
    c.setDash([], 0)
    c.line(x, y + 4.2, x + 26, y + 4.2)
    c.setDash([3.5, 2.2], 0)
    c.line(x, y + 1.6, x + 26, y + 1.6)
    c.setDash([], 0)


def _poly_swatch(c: Canvas, x: float, y: float, fill: str, stroke: str, *, hatch=False):
    """Swatch polígono; hatch recortado al interior del rectángulo."""
    w, h = 26.0, 8.0
    y0 = y - 1.0
    c.setFillColor(HexColor(fill))
    c.setStrokeColor(HexColor(stroke))
    c.setLineWidth(0.8)
    c.rect(x, y0, w, h, stroke=1, fill=1)
    if hatch:
        c.saveState()
        clip = c.beginPath()
        clip.rect(x, y0, w, h)
        c.clipPath(clip, stroke=0, fill=0)
        c.setStrokeColor(HexColor(stroke))
        c.setLineWidth(0.45)
        # Diagonal \ dentro del cuadro (espaciado estable).
        for i in range(-10, int(w) + 12, 3):
            c.line(x + i, y0, x + i + h, y0 + h)
        c.restoreState()
        # Contorno encima del rayado
        c.setStrokeColor(HexColor(stroke))
        c.setLineWidth(0.8)
        c.rect(x, y0, w, h, stroke=1, fill=0)


def _point_swatch(c: Canvas, x: float, y: float):
    c.setFillColor(black)
    c.circle(x + 13, y + 3, 2.2, fill=1, stroke=0)


def _ageb_swatch(c: Canvas, x: float, y: float, font_size: float = 9.0):
    """Óvalo AGEB en tira; tamaño mayor pero acotado al hueco de líderes (~38 pt)."""
    fs = max(7.5, min(10.5, float(font_size)))
    # Ancho/alto del óvalo (cabe en swatch_w≈38)
    ow, oh = 36.0, 14.0
    c.setStrokeColor(HexColor("#FF0000"))
    c.setFillColor(white)
    c.setLineWidth(1.1)
    c.ellipse(x + 1, y - 2.5, x + 1 + ow, y - 2.5 + oh, stroke=1, fill=1)
    c.setFillColor(HexColor("#FF0000"))
    c.setFont("Helvetica-Bold", fs)
    c.drawCentredString(x + 1 + ow / 2.0, y + 1.2, "000-0")


def _draw_index(
    c: Canvas,
    frame: Box,
    geom: Optional[BaseGeometry],
) -> None:
    c.setStrokeColor(HexColor("#424242"))
    c.setFillColor(white)
    c.setLineWidth(0.9)
    c.rect(frame.x, frame.y, frame.width, frame.height, stroke=1, fill=1)
    if geom is None or geom.is_empty:
        return
    try:
        g = geom
        if g.geom_type == "GeometryCollection":
            polys = [p for p in g.geoms if isinstance(p, (Polygon, MultiPolygon))]
            if not polys:
                return
            from shapely.ops import unary_union

            g = unary_union(polys)
        minx, miny, maxx, maxy = g.bounds
        pad = max((maxx - minx), (maxy - miny)) * 0.08 + 1.0
        bounds = (minx - pad, miny - pad, maxx + pad, maxy + pad)
        inner = Box(frame.x + 4, frame.y + 4, frame.width - 8, frame.height - 8)

        def _draw_poly(poly: Polygon):
            rings = [list(poly.exterior.coords)]
            path = c.beginPath()
            for ring in rings:
                if not ring:
                    continue
                px0, py0 = world_to_page(float(ring[0][0]), float(ring[0][1]), bounds, inner)
                path.moveTo(px0, py0)
                for x, y in ring[1:]:
                    px, py = world_to_page(float(x), float(y), bounds, inner)
                    path.lineTo(px, py)
                path.close()
            c.setStrokeColor(black)
            c.setFillColor(HexColor("#ECEFF1"))
            c.setLineWidth(1.0)
            c.drawPath(path, stroke=1, fill=1)

        if isinstance(g, Polygon):
            _draw_poly(g)
        elif isinstance(g, MultiPolygon):
            for p in g.geoms:
                _draw_poly(p)
    except Exception:
        return


def draw_croquis_panel(
    c: Canvas,
    box: Box,
    content: CroquisPanelContent,
    *,
    legend_items: Optional[Sequence[Any]] = None,
) -> None:
    """Dibuja el panel derecho; huecos iguales entre bloques principales."""
    del legend_items
    t = _fs(box)
    pad = t["pad"]
    x0 = box.x + pad
    x1 = box.x2 - pad
    content_w = x1 - x0
    # Fecha / advertencia levantadas del borde inferior
    bottom_lift = max(48.0, pad * 3.5)
    bottom = box.y + pad + bottom_lift

    g: dict[str, Canvas] = {"c": c}

    g["c"].setFillColor(white)
    g["c"].setStrokeColor(HexColor("#424242"))
    g["c"].setLineWidth(1.1)
    g["c"].rect(box.x, box.y, box.width, box.height, stroke=1, fill=1)

    def block_header(y: float) -> float:
        cv = g["c"]
        y = _draw_logos(cv, box, y, pad)
        y -= max(12.0, t["section_gap"] * 1.5)
        cv.setFillColor(black)
        cv.setFont("Helvetica-Bold", t["title"])
        for line in _wrap(cv, content.titulo, "Helvetica-Bold", t["title"], content_w):
            cv.drawCentredString(box.x + box.width / 2.0, y - t["title"], line)
            y -= t["title"] * 1.35
        return y - t["section_gap"]

    def block_sym(y: float) -> float:
        cv = g["c"]
        sg = t["section_gap"]
        row = t["row"]
        y = _section(cv, x0, y, "SIMBOLOGÍA", t["section"], after=sg * 0.35)
        y = _section(cv, x0, y, "Vías de comunicación", t["small"], after=sg)
        y = _leaders(
            cv, x0, y, "Carretera", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _line_swatch(cv, sx, sy, "#212121", 2.2),
        )
        y = _leaders(
            cv, x0, y, "Carretera", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _mixed_double_swatch(cv, sx, sy),
        )
        y = _leaders(
            cv, x0, y, "Brecha", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _line_swatch(cv, sx, sy, "#212121", 0.9, [1.5, 2.2]),
        )
        y = _leaders(
            cv, x0, y, "Ferrocarril", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _line_swatch(cv, sx, sy, "#212121", 1.2, [2, 2]),
        )
        y = _section(cv, x0, y - sg * 0.4, "Rasgos hidrográficos", t["small"], after=sg)
        y = _leaders(
            cv, x0, y, "Corriente perenne", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _line_swatch(cv, sx, sy, _HYDRO_LINE, 1.3),
        )
        y = _leaders(
            cv, x0, y, "Cuerpo de agua", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _poly_swatch(cv, sx, sy, _HYDRO_FILL, _HYDRO_STROKE),
        )
        y = _section(cv, x0, y - sg * 0.4, "Marco geoestadístico", t["small"], after=sg)
        y = _leaders(
            cv, x0, y, "Estatal", x1, t["body"], row=row, swatch_w=34.0,
            draw_swatch=lambda sx, sy: _estatal_cross_swatch(cv, sx, sy),
        )
        y = _leaders(
            cv, x0, y, "Límite municipal", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _line_swatch(cv, sx, sy, "#24C200", 2.8, [8, 6]),
        )
        y = _leaders(
            cv, x0, y, "AGEB rural", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _line_swatch(cv, sx, sy, "#FFAA00", 2.6, [7, 4]),
        )
        y = _leaders(
            cv, x0, y, "Localidad urbana", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _poly_swatch(cv, sx, sy, _URBAN_FILL, "#B2B2B2"),
        )
        y = _leaders(
            cv, x0, y, "Rural amanzanada", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _poly_swatch(
                cv, sx, sy, _URBAN_FILL, "#B2B2B2", hatch=True
            ),
        )
        y = _leaders(
            cv, x0, y, "Rural (punto)", x1, t["body"], row=row,
            draw_swatch=lambda sx, sy: _point_swatch(cv, sx, sy),
        )
        return y

    def block_claves(y: float) -> float:
        cv = g["c"]
        sg = t["section_gap"]
        row = t["row"]
        y = _section(cv, x0, y, "CLAVES", t["section"], after=sg)
        for lab, sample, color in (
            ("Estatal", "00", "#C62828"),
            ("Municipal", "000", "#24C200"),
            ("Localidad urbana", "0000", "#212121"),
            ("Localidad rural", "0000", "#616161"),
        ):
            y = _leaders(
                cv, x0, y, lab, x1, t["body"], row=row,
                draw_swatch=_clave_sample_drawer(cv, sample, color, t["body"]),
            )
        return _leaders(
            cv, x0, y, "AGEB", x1, t["body"], row=row * 1.15, swatch_w=40.0,
            draw_swatch=lambda sx, sy: _ageb_swatch(cv, sx, sy, max(9.0, t["body"] * 0.78)),
        )

    def block_id(y: float) -> float:
        cv = g["c"]
        sg = t["section_gap"]
        id_fs = t["id"]
        lab_fs = t["id_label"]
        y = _section(
            cv, x0, y, "IDENTIFICACIÓN GEOESTADÍSTICA", t["section"], after=sg,
            center_x=box.x + box.width / 2.0,
        )
        cv.setFillColor(black)
        cv.setFont("Helvetica-Bold", lab_fs)
        cv.drawString(x0, y, "ENTIDAD")
        y -= lab_fs * 1.25
        ent_val = f"{content.entidad} {content.cve_ent}".strip()
        cv.setFont("Helvetica-Bold", id_fs)
        for line in _wrap(cv, ent_val, "Helvetica-Bold", id_fs, content_w):
            cv.drawString(x0, y, line)
            y -= id_fs * 1.28
        y -= sg * 0.35
        cv.setFont("Helvetica-Bold", lab_fs)
        cv.drawString(x0, y, "MUNICIPIO")
        y -= lab_fs * 1.25
        mun_val = f"{content.municipio} {content.cve_mun}".strip()
        cv.setFont("Helvetica-Bold", id_fs)
        for line in _wrap(cv, mun_val, "Helvetica-Bold", id_fs, content_w):
            cv.drawString(x0, y, line)
            y -= id_fs * 1.28
        return y - sg * 0.2

    def block_index(y: float) -> float:
        cv = g["c"]
        sg = t["section_gap"]
        y = _section(cv, x0, y, "ÍNDICE DE ARMADO", t["section"], after=sg)
        idx_w = content_w
        idx_h = min(idx_w * 0.88, max(140.0, box.height * 0.12), 220.0)
        idx_box = Box(x0, y - idx_h, idx_w, idx_h)
        _draw_index(cv, idx_box, content.index_geom)
        y = idx_box.y - sg
        esc = float(content.escala or 0.0)
        cv.setFillColor(black)
        cv.setFont("Helvetica-Bold", t["body"])
        if esc > 0:
            cv.drawCentredString(
                box.x + box.width / 2.0,
                y - t["body"],
                f"Escala 1 : {int(round(esc)):,}".replace(",", " "),
            )
        y -= t["body"] * 1.9
        scale_h = 58.0
        scale_box = Box(x0, y - scale_h, content_w, scale_h)
        if esc > 0:
            draw_scale_bar(cv, scale_box, esc, width_ratio=0.98, align="center")
        y = scale_box.y - max(26.0, sg * 3.2)
        north_h = max(64.0, content_w * 0.32)
        north_w = min(content_w * 0.50, north_h * 0.85)
        north_box = Box(
            box.x + (box.width - north_w) / 2.0, y - north_h, north_w, north_h
        )
        draw_north_arrow(cv, north_box)
        return north_box.y - sg * 0.4

    def block_ref(y: float) -> float:
        cv = g["c"]
        sg = t["section_gap"]
        y = _section(cv, x0, y, "REFERENCIA GEOGRÁFICA", t["section"], after=sg)
        for key, val in (
            ("ELIPSOIDE:", content.elipsoide),
            ("PROYECCIÓN:", content.proyeccion),
            ("DATUM:", content.datum),
        ):
            cv.setFont("Helvetica-Bold", t["small"])
            cv.setFillColor(black)
            cv.drawString(x0, y, key)
            key_w = cv.stringWidth(f"{key} ", "Helvetica-Bold", t["small"])
            cv.setFont("Helvetica", t["small"])
            val_s = str(val or "").strip()
            if cv.stringWidth(val_s, "Helvetica", t["small"]) <= content_w - key_w - 2:
                cv.drawRightString(x1, y, val_s)
                y -= t["small"] * 1.7
            else:
                y -= t["small"] * 1.35
                for line in _wrap(cv, val_s, "Helvetica", t["small"], content_w):
                    cv.drawRightString(x1, y, line)
                    y -= t["small"] * 1.45
        return y

    def _warn_body() -> str:
        warn = str(content.advertencia or ADVERTENCIA_TEXT).strip()
        up = warn.upper()
        if up.startswith("ADVERTENCIA:"):
            warn = warn.split(":", 1)[1].strip()
        fecha_s = str(content.fecha_actualizacion or "").strip()
        if "FECHA DE ACTUALIZACIÓN" in warn.upper() and fecha_s:
            idx = warn.upper().find("FECHA DE ACTUALIZACIÓN")
            if idx > 0:
                warn = warn[:idx].strip().rstrip(".")
        return warn.upper()

    def _estimate_warn_height() -> float:
        sg = t["section_gap"]
        body = _warn_body()
        lines = _wrap(g["c"], body, "Helvetica", t["tiny"], content_w) or [""]
        return t["section"] * 2.25 + sg + len(lines) * t["tiny"] * 1.42 + 4.0

    def draw_warn(y_top: float, y_bottom: float) -> None:
        cv = g["c"]
        sg = t["section_gap"]
        y = _section(cv, x0, y_top, "ADVERTENCIA", t["section"], after=sg)
        _draw_justified(
            cv, _warn_body(), "Helvetica", t["tiny"], x0, y, content_w,
            bottom=y_bottom, leading=t["tiny"] * 1.42,
        )

    fecha = str(content.fecha_actualizacion or "").strip()
    fecha_h = t["tiny"] * 2.4 if fecha else 0.0
    warn_h = _estimate_warn_height()
    # Zona inferior fija (advertencia + fecha), ya levantada con bottom_lift
    warn_top = bottom + fecha_h + warn_h
    content_floor = warn_top + 16.0

    # --- Medición (canvas descartable) para huecos iguales ---
    content_blocks: list[Callable[[float], float]] = [
        block_sym,
        block_claves,
        block_id,
        block_index,
        block_ref,
    ]
    g["c"] = Canvas(BytesIO())
    y_m = block_header(box.y2)
    y_after_header = y_m
    heights: list[float] = []
    for fn in content_blocks:
        y0 = y_m
        y_m = fn(y_m)
        heights.append(max(1.0, y0 - y_m))
    g["c"] = c  # restaurar canvas real

    # Redibujar marco (el measure no tocó el real; el marco ya está)
    total_blocks_h = sum(heights)
    avail = max(40.0, y_after_header - content_floor)
    n_gaps = max(1, len(content_blocks) - 1)
    equal_gap = (avail - total_blocks_h) / n_gaps
    # Mismo aire entre los 5 bloques; si falta espacio, reducir hueco (no inventar)
    if equal_gap > 56.0:
        equal_gap = 56.0
    elif equal_gap < 0.0:
        equal_gap = 0.0
    elif equal_gap < 8.0:
        equal_gap = equal_gap  # hueco mínimo natural

    y = block_header(box.y2)
    for i, fn in enumerate(content_blocks):
        y = fn(y)
        if i < len(content_blocks) - 1:
            y -= equal_gap

    draw_warn(warn_top, bottom + fecha_h + 2.0)
    if fecha:
        c.setFont("Helvetica-Bold", t["tiny"])
        c.setFillColor(black)
        c.drawCentredString(
            box.x + box.width / 2.0,
            bottom,
            f"FECHA DE ACTUALIZACIÓN: {fecha.upper()}",
        )
