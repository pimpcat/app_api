"""Clasificación de servicios puntuales SIP (simbología GroSIG / ArcMap)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from reportlab.lib.colors import HexColor, black, white
from reportlab.pdfgen.canvas import Canvas

from cartography_engine.renderers import world_to_page


@dataclass(frozen=True)
class SipClass:
    key: str
    label: str
    color: str
    keywords: tuple[str, ...]


# Orden = prioridad de match (más específico primero)
SIP_CLASSES: tuple[SipClass, ...] = (
    SipClass(
        "deporte",
        "INST. DEPORTIVA / RECREATIVA",
        "#2E7D32",
        (
            "deportiv",
            "recreativ",
            "cancha",
            "estadio",
            "gimnasio",
            "alberca",
            "unidad deportiva",
            "campo de",
            "olimpic",
            "olímpic",
        ),
    ),
    SipClass(
        "tanque",
        "TANQUE DE AGUA",
        "#2E7D32",
        ("tanque", "tinaco", "cisterna", "torre de agua", "caja de agua"),
    ),
    SipClass("iglesia", "TEMPLO / IGLESIA", "#2E7D32", ("iglesia", "templo", "capilla", "santuario")),
    SipClass(
        "escuela",
        "ESCUELA",
        "#2E7D32",
        ("escuela", "escolar", "jardin de ninos", "jardín de niños", "preescolar", "universidad", "colegio"),
    ),
    SipClass(
        "asistencia",
        "ASISTENCIA MÉDICA",
        "#2E7D32",
        ("asistenc", "medico", "médico", "clinica", "clínica", "hospital", "salud", "cruz roja"),
    ),
    SipClass(
        "palacio",
        "INST. GUBERNAMENTAL",
        "#2E7D32",
        ("palacio", "ayuntamien", "presidencia", "gobierno", "delegacion", "delegación", "gubernamental"),
    ),
    SipClass("mercado", "MERCADO", "#2E7D32", ("mercado", "tianguis", "comercio")),
    SipClass("cementerio", "CEMENTERIO", "#2E7D32", ("cementerio", "panteon", "panteón")),
    SipClass(
        "plaza",
        "PLAZA O JARDÍN",
        "#2E7D32",
        ("plaza", "jardin", "jardín", "parque", "area verde", "área verde"),
    ),
    SipClass("metro", "METRO / TREN", "#00838F", ("metro", "tren", "estacion", "estación", "ferroviar")),
    SipClass(
        "subestacion",
        "SUBESTACIÓN ELÉCTRICA",
        "#B71C1C",
        ("subestaci", "electrica", "eléctrica", "transformador"),
    ),
)

_DEFAULT = SipClass("otro", "OTRO SERVICIO", "#616161", ())


def classify_sip_text(*parts: str) -> SipClass:
    blob = " ".join(str(p or "") for p in parts).lower()
    for cls in SIP_CLASSES:
        if any(k in blob for k in cls.keywords):
            return cls
    return _DEFAULT


def draw_sip_glyph(c: Canvas, x: float, y: float, key: str, color: str, size: float = 3.6) -> None:
    """Icono vectorial por clase SIP (trazo fino, estilo ArcMap)."""
    c.saveState()
    fill = HexColor(color)
    c.setStrokeColor(fill)
    c.setFillColor(fill)
    # Trazo fino (PLU): líneas delgadas en verdes y demás
    lw = max(0.12, size * 0.045)
    c.setLineWidth(lw)
    c.setLineCap(1)
    c.setLineJoin(1)
    s = size
    if key == "deporte":
        c.setLineWidth(max(0.12, s * 0.05))
        c.setFillColor(white)
        r = s * 0.38
        gap = r * 1.55
        top_y = y + r * 0.55
        bot_y = y - r * 0.55
        centers = [
            (x - gap, top_y),
            (x, top_y),
            (x + gap, top_y),
            (x - gap * 0.5, bot_y),
            (x + gap * 0.5, bot_y),
        ]
        for cx, cy in centers:
            c.circle(cx, cy, r, stroke=1, fill=0)
    elif key == "tanque":
        # Torre/tanque elevado (referencia ArcMap): depósito oval + patas en celosía
        c.setFillColor(white)
        c.setLineWidth(max(0.12, s * 0.045))
        c.ellipse(x - s * 0.85, y + s * 0.35, x + s * 0.85, y + s * 1.15, stroke=1, fill=0)
        # Patas
        for dx in (-0.55, -0.18, 0.18, 0.55):
            c.line(x + dx * s * 0.55, y + s * 0.35, x + dx * s, y - s * 0.95)
        # Travesaños
        for ty in (0.05, -0.35, -0.7):
            c.line(x - s * 0.7, y + s * ty, x + s * 0.7, y + s * ty)
    elif key == "iglesia":
        # Cruz latina: trazo horizontal ALTO (no al centro)
        c.setLineWidth(max(0.14, s * 0.055))
        c.line(x, y - s * 0.95, x, y + s * 1.1)
        c.line(x - s * 0.7, y + s * 0.55, x + s * 0.7, y + s * 0.55)
    elif key == "escuela":
        # Bandera / asta (referencia ArcMap)
        c.setLineWidth(max(0.13, s * 0.05))
        c.line(x - s * 0.15, y - s * 0.95, x - s * 0.15, y + s * 1.05)
        path = c.beginPath()
        path.moveTo(x - s * 0.15, y + s * 1.05)
        path.lineTo(x + s * 1.05, y + s * 0.55)
        path.lineTo(x - s * 0.15, y + s * 0.15)
        path.close()
        c.setFillColor(white)
        c.drawPath(path, fill=1, stroke=1)
    elif key == "asistencia":
        # Cruz griega: trazo horizontal AL CENTRO
        c.setLineWidth(max(0.14, s * 0.055))
        c.line(x - s * 0.95, y, x + s * 0.95, y)
        c.line(x, y - s * 0.95, x, y + s * 0.95)
    elif key == "palacio":
        c.setLineWidth(max(0.12, s * 0.045))
        c.rect(x - s * 1.0, y - s * 0.5, s * 2.0, s * 1.0, stroke=1, fill=0)
        for dx in (-0.5, 0, 0.5):
            c.line(x + dx * s, y - s * 0.5, x + dx * s, y + s * 0.5)
        c.line(x - s * 1.0, y + s * 0.5, x - s * 1.0, y + s * 0.85)
        c.line(x + s * 1.0, y + s * 0.5, x + s * 1.0, y + s * 0.85)
        c.line(x - s * 1.0, y + s * 0.85, x + s * 1.0, y + s * 0.85)
    elif key == "mercado":
        c.circle(x, y, s * 0.9, stroke=1, fill=0)
        c.setLineWidth(max(0.13, s * 0.05))
        c.line(x - s * 0.5, y, x + s * 0.5, y)
        c.line(x, y - s * 0.3, x, y + s * 0.3)
    elif key == "cementerio":
        c.rect(x - s * 0.9, y - s * 0.9, s * 1.8, s * 1.8, stroke=1, fill=0)
        c.setLineWidth(max(0.13, s * 0.05))
        c.line(x, y - s * 0.5, x, y + s * 0.5)
        c.line(x - s * 0.4, y, x + s * 0.4, y)
    elif key == "plaza":
        c.setLineWidth(max(0.12, s * 0.045))
        c.circle(x, y - s * 0.1, s * 0.45, stroke=0, fill=1)
        c.setStrokeColor(fill)
        for a in (-0.85, 0, 0.85):
            c.line(x, y + s * 0.15, x + a * s * 0.9, y + s * 1.0)
    elif key == "metro":
        c.circle(x, y, s * 0.95, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", max(3.5, s * 1.2))
        c.drawCentredString(x, y - s * 0.35, "M")
    elif key == "subestacion":
        c.circle(x, y, s * 0.75, stroke=1, fill=0)
        c.line(x - s * 1.2, y, x + s * 1.2, y)
    else:
        c.setFillColor(white)
        c.circle(x, y, s * 0.65, stroke=1, fill=1)
    c.restoreState()


def _sip_glyph_size(frame, map_scale: float | None = None) -> float:
    base = max(2.6, min(5.2, getattr(frame, "width", 800) * 0.0026))
    s = float(map_scale or 0.0)
    if s >= 50000:
        dens = 0.32
    elif s >= 35000:
        dens = 0.4
    elif s >= 20000:
        dens = 0.52
    elif s >= 12000:
        dens = 0.7
    else:
        dens = 0.9
    return max(1.2, base * dens)


def sip_page_obstacles(
    features: Sequence[dict],
    bounds: tuple[float, float, float, float],
    frame,
    *,
    max_points: int = 800,
    map_scale: float | None = None,
) -> list[tuple[float, float, float, float]]:
    """Cajas en coords de página para que las etiquetas no se encimen con SIP."""
    boxes: list[tuple[float, float, float, float]] = []
    glyph = _sip_glyph_size(frame, map_scale)
    n = 0
    for feat in features:
        if n >= max_points:
            break
        geom = feat.get("geometry")
        if geom is None or getattr(geom, "is_empty", True):
            continue
        try:
            if geom.geom_type == "Point":
                wx, wy = float(geom.x), float(geom.y)
            else:
                p = geom.representative_point()
                wx, wy = float(p.x), float(p.y)
        except Exception:
            continue
        px, py = world_to_page(wx, wy, bounds, frame)
        pad = glyph * 2.2
        boxes.append((px - pad, py - pad, pad * 2, pad * 2))
        n += 1
    return boxes


def draw_sip_features_on_map(
    c: Canvas,
    features: Sequence[dict],
    bounds: tuple[float, float, float, float],
    frame,
    *,
    max_points: int = 800,
    map_scale: float | None = None,
) -> None:
    n = 0
    glyph = _sip_glyph_size(frame, map_scale)
    for feat in features:
        if n >= max_points:
            break
        geom = feat.get("geometry")
        if geom is None or getattr(geom, "is_empty", True):
            continue
        try:
            if geom.geom_type == "Point":
                wx, wy = float(geom.x), float(geom.y)
            else:
                p = geom.representative_point()
                wx, wy = float(p.x), float(p.y)
        except Exception:
            continue
        px, py = world_to_page(wx, wy, bounds, frame)
        cls: SipClass = feat.get("sip_class") or _DEFAULT
        draw_sip_glyph(c, px, py, cls.key, cls.color, size=glyph)
        n += 1


def legend_sip_classes() -> list[SipClass]:
    return list(SIP_CLASSES)
