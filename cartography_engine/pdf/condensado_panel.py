"""Panel lateral del condensado estatal (plotter 120×90).

Inspirado en `croquis_panel.py` (90×70). Escala tipográfica ≈ √(área):
  √((120×90)/(90×70)) ≈ 1.309 respecto al croquis.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Callable, Optional, Sequence

from reportlab.lib.colors import HexColor, black, white
from reportlab.pdfgen.canvas import Canvas
from shapely.geometry.base import BaseGeometry

from cartography_engine.layouts import Box
from cartography_engine.pdf.croquis_panel import (
    _URBAN_FILL,
    _clave_sample_drawer,
    _draw_index,
    _draw_justified,
    _draw_logos,
    _estatal_cross_swatch,
    _leaders,
    _line_swatch,
    _poly_swatch,
    _section,
    _wrap,
)
from cartography_engine.renderers import draw_north_arrow, draw_scale_bar
from cartography_engine.symbols.legal_texts import ADVERTENCIA_TEXT

# Colores condensado (mapa + panel)
_COND_HYDRO = "#00ADEE"
_COND_HYDRO_FILL = "#7DD4F7"
_COND_MUN = "#4F9A58"
_COND_EST = "#EE1C25"

# Proporción lineal de papel: croquis 90×70 → condensado 120×90
_PAPER_SCALE = ((120.0 * 90.0) / (90.0 * 70.0)) ** 0.5  # ≈ 1.309


@dataclass(frozen=True)
class CondensadoPanelContent:
    titulo: str = "CONDENSADO ESTATAL CON MARCO GEOESTADÍSTICO"
    entidad: str = "Guerrero"
    cve_ent: str = "12"
    escala: float = 0.0
    advertencia: str = ADVERTENCIA_TEXT
    fecha_actualizacion: str = "DICIEMBRE DEL 2024"
    index_geom: Optional[BaseGeometry] = None
    elipsoide: str = "GRS80"
    proyeccion: str = "CÓNICA CONFORME DE LAMBERT"
    datum: str = "ITRF2008 ÉPOCA 2010.0"


def _fs(box: Box) -> dict[str, float]:
    """Tipografía escalada al plotter 120×90 (base legend ~340 pt)."""
    w_k = max(0.95, min(1.18, box.width / 340.0))
    k = _PAPER_SCALE * w_k
    return {
        "title": round(18.5 * k, 1),
        "section": round(14.5 * k, 1),
        "body": round(12.5 * k, 1),
        "small": round(11.0 * k, 1),
        "tiny": round(8.8 * k, 1),
        "id": round(16.0 * k, 1),
        "id_label": round(12.5 * k, 1),
        "pad": max(12.0, 11.0 * k),
        "section_gap": round(7.5 * k, 1),
        "row": round(12.5 * k * 1.62, 1),
        "swatch": round(28.0 * k, 1),
        "paper_k": k,
    }


def _double_road_swatch(c: Canvas, x: float, y: float, color: str = "#212121", k: float = 1.3):
    c.setStrokeColor(HexColor(color))
    c.setLineWidth(2.4 * k / 1.3)
    c.setDash([], 0)
    c.setLineCap(1)
    c.line(x, y + 3.2 * k / 1.3, x + 26 * k / 1.3, y + 3.2 * k / 1.3)
    c.setStrokeColor(white)
    c.setLineWidth(1.1 * k / 1.3)
    c.line(x, y + 3.2 * k / 1.3, x + 26 * k / 1.3, y + 3.2 * k / 1.3)
    c.setStrokeColor(HexColor(color))


def _plane_swatch(c: Canvas, x: float, y: float, size: float = 5.0):
    """Glifo simple de avión (coincide con marker plane del mapa)."""
    s = max(3.5, float(size))
    cx, cy = x + 13, y + 3
    c.setFillColor(black)
    c.setStrokeColor(black)
    c.setLineWidth(0.7)
    path = c.beginPath()
    path.moveTo(cx, cy + s * 0.55)
    path.lineTo(cx - s * 0.55, cy - s * 0.35)
    path.lineTo(cx, cy - s * 0.1)
    path.lineTo(cx + s * 0.55, cy - s * 0.35)
    path.close()
    c.drawPath(path, fill=1, stroke=0)
    c.line(cx - s * 0.7, cy + s * 0.05, cx + s * 0.7, cy + s * 0.05)


def draw_condensado_panel(
    c: Canvas,
    box: Box,
    content: CondensadoPanelContent,
    *,
    legend_items: Optional[Sequence[Any]] = None,
) -> None:
    """Panel derecho del condensado; simbología acotada al producto reducido."""
    del legend_items
    t = _fs(box)
    pad = t["pad"]
    x0 = box.x + pad
    x1 = box.x2 - pad
    content_w = x1 - x0
    sw = t["swatch"]
    pk = float(t["paper_k"])
    bottom_lift = max(56.0, pad * 3.2)
    bottom = box.y + pad + bottom_lift

    g: dict[str, Canvas] = {"c": c}

    g["c"].setFillColor(white)
    g["c"].setStrokeColor(HexColor("#424242"))
    g["c"].setLineWidth(1.25)
    g["c"].rect(box.x, box.y, box.width, box.height, stroke=1, fill=1)

    def block_header(y: float) -> float:
        cv = g["c"]
        y = _draw_logos(cv, box, y, pad)
        y -= max(14.0, t["section_gap"] * 1.4)
        cv.setFillColor(black)
        cv.setFont("Helvetica-Bold", t["title"])
        for line in _wrap(cv, content.titulo, "Helvetica-Bold", t["title"], content_w):
            cv.drawCentredString(box.x + box.width / 2.0, y - t["title"], line)
            y -= t["title"] * 1.32
        return y - t["section_gap"]

    def block_sym(y: float) -> float:
        cv = g["c"]
        sg = t["section_gap"]
        row = t["row"]
        y = _section(cv, x0, y, "SIMBOLOGÍA", t["section"], after=sg * 0.35)
        y = _section(cv, x0, y, "Vías de comunicación", t["small"], after=sg)
        y = _leaders(
            cv, x0, y, "Carretera (doble línea)", x1, t["body"], row=row, swatch_w=sw,
            draw_swatch=lambda sx, sy: _double_road_swatch(cv, sx, sy, k=pk),
        )
        from cartography_engine.pdf.croquis_panel import _mixed_double_swatch

        y = _leaders(
            cv, x0, y, "Carretera (mixta)", x1, t["body"], row=row, swatch_w=sw,
            draw_swatch=lambda sx, sy: _mixed_double_swatch(cv, sx, sy),
        )
        y = _leaders(
            cv, x0, y, "Carretera", x1, t["body"], row=row, swatch_w=sw,
            draw_swatch=lambda sx, sy: _line_swatch(cv, sx, sy, "#212121", 1.2),
        )
        y = _section(cv, x0, y - sg * 0.35, "Rasgos hidrográficos", t["small"], after=sg)
        y = _leaders(
            cv, x0, y, "Corriente perenne", x1, t["body"], row=row, swatch_w=sw,
            draw_swatch=lambda sx, sy: _line_swatch(cv, sx, sy, _COND_HYDRO, 1.5),
        )
        y = _leaders(
            cv, x0, y, "Cuerpo de agua perenne", x1, t["body"], row=row, swatch_w=sw,
            draw_swatch=lambda sx, sy: _poly_swatch(
                cv, sx, sy, _COND_HYDRO_FILL, _COND_HYDRO
            ),
        )
        y = _section(cv, x0, y - sg * 0.35, "Marco geoestadístico", t["small"], after=sg)
        y = _leaders(
            cv, x0, y, "Estatal", x1, t["body"], row=row, swatch_w=max(sw, 36.0),
            draw_swatch=lambda sx, sy: _estatal_cross_swatch(cv, sx, sy, _COND_EST),
        )
        y = _leaders(
            cv, x0, y, "Límite municipal", x1, t["body"], row=row, swatch_w=sw,
            draw_swatch=lambda sx, sy: _line_swatch(cv, sx, sy, _COND_MUN, 2.6, [8, 6]),
        )
        y = _leaders(
            cv, x0, y, "Localidad urbana", x1, t["body"], row=row, swatch_w=sw,
            draw_swatch=lambda sx, sy: _poly_swatch(cv, sx, sy, _URBAN_FILL, "#B2B2B2"),
        )
        y = _section(cv, x0, y - sg * 0.35, "Infraestructura", t["small"], after=sg)
        y = _leaders(
            cv, x0, y, "Aeropuerto internacional", x1, t["body"], row=row, swatch_w=sw,
            draw_swatch=lambda sx, sy: _plane_swatch(cv, sx, sy, 5.5 * pk / 1.3),
        )
        y = _leaders(
            cv, x0, y, "Aeropuerto nacional / local", x1, t["body"], row=row, swatch_w=sw,
            draw_swatch=lambda sx, sy: _plane_swatch(cv, sx, sy, 4.0 * pk / 1.3),
        )
        return y

    def block_claves(y: float) -> float:
        cv = g["c"]
        sg = t["section_gap"]
        row = t["row"]
        y = _section(cv, x0, y, "CLAVES", t["section"], after=sg)
        for lab, sample, color in (
            ("Estatal", "00", _COND_EST),
            ("Municipal", "000", _COND_MUN),
            ("Localidad urbana", "0000", "#212121"),
        ):
            y = _leaders(
                cv, x0, y, lab, x1, t["body"], row=row, swatch_w=sw,
                draw_swatch=_clave_sample_drawer(cv, sample, color, t["body"]),
            )
        return y

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
        return y - sg * 0.2

    def block_index(y: float) -> float:
        cv = g["c"]
        sg = t["section_gap"]
        y = _section(cv, x0, y, "ÍNDICE DE ARMADO", t["section"], after=sg)
        idx_w = content_w
        idx_h = min(idx_w * 0.92, max(160.0, box.height * 0.13), 260.0)
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
        scale_h = 62.0 * pk / 1.3
        scale_box = Box(x0, y - scale_h, content_w, scale_h)
        if esc > 0:
            draw_scale_bar(cv, scale_box, esc, width_ratio=0.98, align="center")
        y = scale_box.y - max(28.0, sg * 3.0)
        north_h = max(72.0, content_w * 0.34)
        north_w = min(content_w * 0.52, north_h * 0.85)
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
    warn_top = bottom + fecha_h + warn_h
    content_floor = warn_top + 18.0

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
    g["c"] = c

    total_blocks_h = sum(heights)
    avail = max(40.0, y_after_header - content_floor)
    n_gaps = max(1, len(content_blocks) - 1)
    equal_gap = (avail - total_blocks_h) / n_gaps
    if equal_gap > 64.0:
        equal_gap = 64.0
    elif equal_gap < 0.0:
        equal_gap = 0.0

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
