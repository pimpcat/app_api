"""Identidad institucional GroSIG en productos PDF (data-driven vía branding.json)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from reportlab.lib.colors import HexColor
from reportlab.pdfgen.canvas import Canvas

from cartography_engine.branding import get_branding, resolve_logo_paths
from cartography_engine.layouts import Box


def _try_draw_image(c: Canvas, path: Path, x: float, y: float, max_h: float) -> float:
    """Dibuja PNG/JPG si existe. Devuelve el ancho usado (0 si no)."""
    if not path.is_file():
        return 0.0
    try:
        from reportlab.lib.utils import ImageReader

        img = ImageReader(str(path))
        iw, ih = img.getSize()
        if ih <= 0:
            return 0.0
        scale = max_h / float(ih)
        w = float(iw) * scale
        c.drawImage(
            img,
            x,
            y,
            width=w,
            height=max_h,
            mask="auto",
            preserveAspectRatio=True,
            anchor="sw",
        )
        return w
    except Exception:
        return 0.0


def draw_brand_header(c: Canvas, box: Box, subtitle: Optional[str] = None) -> None:
    """Franja superior institucional (logos opcionales + tipografía)."""
    branding = get_branding()
    c.setFillColor(HexColor("#F4F7FA"))
    c.setStrokeColor(HexColor("#1F4E79"))
    c.setLineWidth(0.8)
    c.rect(box.x, box.y, box.width, box.height, stroke=1, fill=1)

    x = box.x + 8
    y = box.y + 6
    max_h = box.height - 12

    used = 0.0
    for path in resolve_logo_paths():
        w = _try_draw_image(c, path, x + used, y, max_h * 0.85)
        if w:
            used += w + 8

    text_x = box.x + 10 + (used if used else 0)
    brand_fs = max(10.0, min(16.0, box.height * 0.28))
    sub_fs = max(8.0, min(12.0, box.height * 0.22))
    c.setFillColor(HexColor("#003366"))
    c.setFont("Helvetica-Bold", brand_fs)
    c.drawString(text_x, box.y + box.height * 0.52, str(branding["brand_line"]))
    c.setFont("Helvetica", sub_fs)
    c.setFillColor(HexColor("#445566"))
    line2 = subtitle or str(branding["engine_line"])
    c.drawString(text_x, box.y + box.height * 0.22, line2[:90])


def draw_brand_footer(c: Canvas, box: Box, text: str) -> None:
    """Pie con identidad + metadatos del producto."""
    c.setStrokeColor(HexColor("#1F4E79"))
    c.setLineWidth(0.75)
    c.line(box.x, box.y2 - 2, box.x2, box.y2 - 2)
    fs = max(8.5, min(13.0, box.height * 0.38))
    c.setFillColor(HexColor("#333333"))
    c.setFont("Helvetica", fs)
    c.drawString(box.x, box.y + box.height * 0.38, text[:180])
    c.setFillColor(HexColor("#667788"))
    c.setFont("Helvetica", max(7.0, fs * 0.85))
    c.drawRightString(
        box.x2,
        box.y + box.height * 0.38,
        "Cartografía vectorial · no es captura del visor",
    )
