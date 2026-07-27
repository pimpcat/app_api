"""Parser QGIS XML → GroSIG Symbol (subset 1.0: SimpleFill / SimpleLine / SimpleMarker)."""

from __future__ import annotations

import re
from typing import Any, Optional
from xml.etree import ElementTree as ET

from cartography_engine.models import CartographyError
from cartography_engine.symbols import (
    LineSymbol,
    PointSymbol,
    PolygonSymbol,
    line_symbol_from_dict,
    point_symbol_from_dict,
    polygon_symbol_from_dict,
)


def _rgba_qgis_to_hex(value: str, default: str = "#333333") -> tuple[str, float]:
    """
    QGIS suele usar 'R,G,B,A' (0-255). Devuelve (#RRGGBB, opacity 0-1).
    """
    raw = str(value or "").strip()
    if not raw:
        return default, 1.0
    if raw.startswith("#") and len(raw) in (4, 7, 9):
        if len(raw) == 9:
            # #RRGGBBAA
            return raw[:7], int(raw[7:9], 16) / 255.0
        return raw[:7], 1.0
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 3:
        try:
            r, g, b = (int(float(parts[0])), int(float(parts[1])), int(float(parts[2])))
            a = int(float(parts[3])) if len(parts) > 3 else 255
            return f"#{r:02X}{g:02X}{b:02X}", max(0.0, min(1.0, a / 255.0))
        except ValueError:
            return default, 1.0
    return default, 1.0


def _option_map(layer_el: ET.Element) -> dict[str, str]:
    opts: dict[str, str] = {}
    # QGIS 3: <Option type="Map"> <Option name="color" value="..."/>
    for opt in layer_el.iter("Option"):
        name = opt.attrib.get("name")
        if not name:
            continue
        if "value" in opt.attrib:
            opts[name] = opt.attrib.get("value") or ""
        elif opt.text and opt.text.strip():
            opts[name] = opt.text.strip()
    # QGIS 2 legacy: <prop k="color" v="..."/>
    for prop in layer_el.iter("prop"):
        k = prop.attrib.get("k")
        if k:
            opts[k] = prop.attrib.get("v") or ""
    return opts


def _first_symbol_layer(root: ET.Element) -> Optional[ET.Element]:
    for el in root.iter("layer"):
        return el
    return None


def parse_qgis_symbol_xml(xml_text: str) -> dict[str, Any]:
    """
    Convierte un fragmento XML de símbolo QGIS a dict GroSIG Symbol.
    """
    text = (xml_text or "").strip()
    if not text:
        raise CartographyError("QGIS_XML_EMPTY", "XML vacío")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CartographyError("QGIS_XML_INVALID", f"XML inválido: {exc}") from exc

    # Puede venir <symbols><symbol>… o <symbol> directo
    symbol_el = root if root.tag.endswith("symbol") else None
    if symbol_el is None:
        for el in root.iter("symbol"):
            symbol_el = el
            break
    if symbol_el is None:
        # Aceptar un <layer> suelto
        symbol_el = root

    layer_el = _first_symbol_layer(symbol_el) if symbol_el is not None else None
    if layer_el is None and symbol_el is not None and symbol_el.tag.endswith("layer"):
        layer_el = symbol_el
    if layer_el is None:
        raise CartographyError("QGIS_NO_LAYER", "No se encontró layer de símbolo QGIS")

    opts = _option_map(layer_el)
    class_name = (layer_el.attrib.get("class") or "").lower()
    symbol_type = (symbol_el.attrib.get("type") if symbol_el is not None else "") or ""
    symbol_type = symbol_type.lower()

    # Inferir tipo
    if "marker" in class_name or symbol_type in ("marker", "point"):
        color, opacity = _rgba_qgis_to_hex(opts.get("color") or opts.get("fill_color") or "#C0392B")
        outline, _ = _rgba_qgis_to_hex(opts.get("outline_color") or "#FFFFFF")
        size = float(opts.get("size") or opts.get("size_map_unit_scale") or 3.5)
        # QGIS size often in mm; aproximación tipográfica
        if size > 12:
            size = size * 0.35
        sym = point_symbol_from_dict(
            {"type": "point", "fill_color": color, "stroke_color": outline, "size": max(1.5, size)}
        )
        return {
            "type": "point",
            "fill_color": sym.fill_color,
            "stroke_color": sym.stroke_color,
            "size": sym.size,
            "source": "qgis",
            "qgis_class": layer_el.attrib.get("class"),
            "opacity": opacity,
        }

    if "line" in class_name or symbol_type in ("line",):
        color, opacity = _rgba_qgis_to_hex(
            opts.get("line_color") or opts.get("outline_color") or opts.get("color") or "#333333"
        )
        width = float(opts.get("line_width") or opts.get("outline_width") or opts.get("width") or 0.8)
        sym = line_symbol_from_dict({"stroke_color": color, "stroke_width": max(0.3, width)})
        return {
            "type": "line",
            "stroke_color": sym.stroke_color,
            "stroke_width": sym.stroke_width,
            "source": "qgis",
            "qgis_class": layer_el.attrib.get("class"),
            "opacity": opacity,
        }

    # Default: fill / polygon
    fill, fill_op = _rgba_qgis_to_hex(opts.get("color") or opts.get("fill_color") or "#D9E8F5")
    outline, _ = _rgba_qgis_to_hex(
        opts.get("outline_color") or opts.get("line_color") or "#1F4E79"
    )
    width = float(opts.get("outline_width") or opts.get("line_width") or 1.0)
    # Preferir alpha del color de relleno
    opacity = fill_op
    if "fill_opacity" in opts:
        try:
            opacity = float(opts["fill_opacity"])
            if opacity > 1:
                opacity = opacity / 100.0
        except ValueError:
            pass
    sym = polygon_symbol_from_dict(
        {
            "fill_color": fill,
            "fill_opacity": opacity,
            "stroke_color": outline,
            "stroke_width": max(0.3, width),
        }
    )
    return {
        "type": "polygon",
        "fill_color": sym.fill_color,
        "fill_opacity": sym.fill_opacity,
        "stroke_color": sym.stroke_color,
        "stroke_width": sym.stroke_width,
        "source": "qgis",
        "qgis_class": layer_el.attrib.get("class"),
    }


def qgis_symbol_to_grosig(xml_text: str) -> dict[str, Any]:
    """Alias público del parser."""
    return parse_qgis_symbol_xml(xml_text)
