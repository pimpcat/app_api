"""Exportación SVG vectorial (sin raster / sin Matplotlib)."""

from __future__ import annotations

import html
from typing import Optional, Sequence
from xml.etree.ElementTree import Element, SubElement, tostring

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

from cartography_engine.branding import get_branding
from cartography_engine.label_collision import resolve_label_collisions
from cartography_engine.layouts import Box, PageLayout
from cartography_engine.layers import LayerData, LegendItem
from cartography_engine.renderers import compute_map_scale, world_to_page
from cartography_engine.symbols import LineSymbol, PointSymbol, PolygonSymbol


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _flip_y(y: float, page_h: float) -> float:
    return page_h - y


def _ring_to_svg_path(ring, bounds, frame: Box, page_h: float) -> str:
    """Un anillo → path SVG con Y invertida (origen arriba)."""
    if not ring:
        return ""
    cmds: list[str] = []
    for i, xy in enumerate(ring):
        x, y = float(xy[0]), float(xy[1])
        px, py = world_to_page(x, y, bounds, frame)
        sy = _flip_y(py, page_h)
        cmds.append(f"{'M' if i == 0 else 'L'} {px:.2f} {sy:.2f}")
    cmds.append("Z")
    return " ".join(cmds)


def _polygon_to_svg_path(geom: Polygon, bounds, frame: Box, page_h: float) -> str:
    parts = [_ring_to_svg_path(list(geom.exterior.coords), bounds, frame, page_h)]
    for interior in geom.interiors:
        parts.append(_ring_to_svg_path(list(interior.coords), bounds, frame, page_h))
    return " ".join(p for p in parts if p)


def _append_geom_svg(
    parent: Element,
    geom: BaseGeometry,
    bounds,
    frame: Box,
    page_h: float,
    symbol,
) -> None:
    if geom is None or geom.is_empty:
        return

    if isinstance(geom, GeometryCollection):
        for g in geom.geoms:
            _append_geom_svg(parent, g, bounds, frame, page_h, symbol)
        return

    if isinstance(geom, (Point, MultiPoint)):
        pts = [geom] if isinstance(geom, Point) else list(geom.geoms)
        fill = getattr(symbol, "fill_color", "#C0392B")
        stroke = getattr(symbol, "stroke_color", "#FFFFFF")
        size = float(getattr(symbol, "size", 3.5))
        r = max(1.2, size * 0.55)
        for pt in pts:
            if not isinstance(pt, Point):
                continue
            px, py = world_to_page(float(pt.x), float(pt.y), bounds, frame)
            SubElement(
                parent,
                "circle",
                {
                    "cx": f"{px:.2f}",
                    "cy": f"{_flip_y(py, page_h):.2f}",
                    "r": f"{r:.2f}",
                    "fill": str(fill),
                    "stroke": str(stroke),
                    "stroke-width": "0.6",
                },
            )
        return

    if isinstance(geom, (LineString, MultiLineString)):
        lines = [geom] if isinstance(geom, LineString) else list(geom.geoms)
        stroke = getattr(symbol, "stroke_color", "#333333")
        width = float(getattr(symbol, "stroke_width", 0.8))
        for line in lines:
            coords = list(line.coords)
            if len(coords) < 2:
                continue
            pts = []
            for x, y in coords:
                px, py = world_to_page(float(x), float(y), bounds, frame)
                pts.append(f"{px:.2f},{_flip_y(py, page_h):.2f}")
            SubElement(
                parent,
                "polyline",
                {
                    "points": " ".join(pts),
                    "fill": "none",
                    "stroke": str(stroke),
                    "stroke-width": f"{width:.2f}",
                },
            )
        return

    if isinstance(geom, MultiPolygon):
        for g in geom.geoms:
            _append_geom_svg(parent, g, bounds, frame, page_h, symbol)
        return

    if isinstance(geom, Polygon):
        d = _polygon_to_svg_path(geom, bounds, frame, page_h)
        if not d or d.strip() == "Z":
            return
        fill = getattr(symbol, "fill_color", "#D9E8F5")
        opacity = float(getattr(symbol, "fill_opacity", 0.55))
        stroke = getattr(symbol, "stroke_color", "#1F4E79")
        sw = float(getattr(symbol, "stroke_width", 1.2))
        SubElement(
            parent,
            "path",
            {
                "d": d,
                "fill": str(fill),
                "fill-opacity": f"{opacity:.2f}",
                "stroke": str(stroke),
                "stroke-width": f"{sw:.2f}",
                "fill-rule": "evenodd",
            },
        )
        return

    # Fallback: descomponer geometrías mixtas (p. ej. GeometryCollection implícita)
    try:
        if hasattr(geom, "geoms"):
            for g in geom.geoms:
                _append_geom_svg(parent, g, bounds, frame, page_h, symbol)
    except Exception:
        pass


def _rect(parent, box: Box, page_h: float, **attrs):
    SubElement(
        parent,
        "rect",
        {
            "x": f"{box.x:.2f}",
            "y": f"{_flip_y(box.y2, page_h):.2f}",
            "width": f"{box.width:.2f}",
            "height": f"{box.height:.2f}",
            **attrs,
        },
    )


def _text(parent, x: float, y: float, page_h: float, text: str, **attrs):
    el = SubElement(
        parent,
        "text",
        {
            "x": f"{x:.2f}",
            "y": f"{_flip_y(y, page_h):.2f}",
            **attrs,
        },
    )
    el.text = text


def render_svg(
    *,
    layout: PageLayout,
    title: str,
    footer: str,
    bounds: Optional[tuple[float, float, float, float]] = None,
    layers: Optional[Sequence[LayerData]] = None,
    legend_items: Optional[Sequence[LegendItem]] = None,
    map_scale: Optional[float] = None,
    demo: bool = False,
    brand_subtitle: Optional[str] = None,
    labels: Optional[Sequence[dict]] = None,
    max_labels: int = 60,
) -> bytes:
    page_h = layout.page_height
    page_w = layout.page_width
    root = Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "width": f"{page_w:.2f}",
            "height": f"{page_h:.2f}",
            "viewBox": f"0 0 {page_w:.2f} {page_h:.2f}",
        },
    )
    SubElement(root, "title").text = title
    SubElement(
        root,
        "desc",
    ).text = "GroSIG Cartography Engine · salida SVG vectorial · no es captura del visor"

    # Fondo
    SubElement(
        root,
        "rect",
        {"x": "0", "y": "0", "width": f"{page_w:.2f}", "height": f"{page_h:.2f}", "fill": "#FFFFFF"},
    )

    if layout.show_outer_frame:
        _rect(root, layout.outer_frame, page_h, fill="none", stroke="#000000", **{"stroke-width": "1.8"})
    if layout.show_brand_header and layout.brand_header.height > 0:
        branding = get_branding()
        _rect(root, layout.brand_header, page_h, fill="#F4F7FA", stroke="#1F4E79", **{"stroke-width": "0.8"})
        _text(
            root,
            layout.brand_header.x + 10,
            layout.brand_header.y + layout.brand_header.height * 0.55,
            page_h,
            str(branding["brand_line"]),
            fill="#003366",
            **{"font-family": "Helvetica, Arial, sans-serif", "font-size": "9", "font-weight": "bold"},
        )
        _text(
            root,
            layout.brand_header.x + 10,
            layout.brand_header.y + layout.brand_header.height * 0.22,
            page_h,
            brand_subtitle or str(branding["engine_line"]),
            fill="#445566",
            **{"font-family": "Helvetica, Arial, sans-serif", "font-size": "7.5"},
        )

    _rect(root, layout.map_frame, page_h, fill="none", stroke="#000000", **{"stroke-width": "0.9"})
    _text(
        root,
        layout.title.x + layout.title.width / 2,
        layout.title.y + layout.title.height * 0.45,
        page_h,
        title[:120],
        fill="#000000",
        **{
            "font-family": "Helvetica, Arial, sans-serif",
            "font-size": "14",
            "font-weight": "bold",
            "text-anchor": "middle",
        },
    )

    map_g = SubElement(root, "g", {"id": "map"})
    if demo and bounds is None:
        # bounds sintéticos no aplican; dibujar formas en page space
        mf = layout.map_frame
        pad = 24
        poly = [
            (mf.x + mf.width * 0.15, mf.y + mf.height * 0.20),
            (mf.x + mf.width * 0.45, mf.y + mf.height * 0.10),
            (mf.x + mf.width * 0.85, mf.y + mf.height * 0.25),
            (mf.x + mf.width * 0.90, mf.y + mf.height * 0.70),
            (mf.x + mf.width * 0.55, mf.y + mf.height * 0.90),
            (mf.x + mf.width * 0.20, mf.y + mf.height * 0.75),
        ]
        pts = " ".join(f"{x:.2f},{_flip_y(y, page_h):.2f}" for x, y in poly)
        SubElement(
            map_g,
            "polygon",
            {
                "points": pts,
                "fill": "#D9E8F5",
                "fill-opacity": "0.6",
                "stroke": "#1F4E79",
                "stroke-width": "1.5",
            },
        )
        for x, y in (
            (mf.x + mf.width * 0.35, mf.y + mf.height * 0.45),
            (mf.x + mf.width * 0.60, mf.y + mf.height * 0.55),
            (mf.x + mf.width * 0.48, mf.y + mf.height * 0.70),
        ):
            SubElement(
                map_g,
                "circle",
                {
                    "cx": f"{x:.2f}",
                    "cy": f"{_flip_y(y, page_h):.2f}",
                    "r": "3",
                    "fill": "#C0392B",
                    "stroke": "#FFFFFF",
                },
            )
        scale_value = float(map_scale or 50000)
    else:
        if not bounds:
            raise ValueError("bounds requerido para SVG con capas")
        for layer in layers or []:
            if not layer.geometry or layer.geometry.is_empty:
                continue
            _append_geom_svg(
                map_g,
                layer.geometry,
                bounds,
                layout.map_frame,
                page_h,
                layer.definition.symbol,
            )
        scale_value = float(map_scale or compute_map_scale(bounds, layout.map_frame))

    # Etiquetas con colisión (1.1)
    if labels and bounds:
        placed = resolve_label_collisions(
            labels,
            bounds=bounds,
            frame=layout.map_frame,
            font_size=6.5,
            padding=2.0,
            max_labels=max(1, min(int(max_labels), 200)),
        )
        for item in placed:
            _text(
                map_g,
                item.x,
                item.y,
                page_h,
                item.text,
                fill="#222222",
                **{"font-family": "Helvetica, Arial, sans-serif", "font-size": "6.5"},
            )

    # Leyenda
    if legend_items and layout.legend:
        lb = layout.legend
        _rect(root, lb, page_h, fill="#FFFFFF", stroke="#888888", **{"stroke-width": "0.7"})
        _text(
            root,
            lb.x + 6,
            lb.y2 - 12,
            page_h,
            "Leyenda",
            fill="#000000",
            **{"font-family": "Helvetica, Arial, sans-serif", "font-size": "8", "font-weight": "bold"},
        )
        y = lb.y2 - 28
        for item in legend_items:
            if y < lb.y + 8:
                break
            if item.kind == "point":
                SubElement(
                    root,
                    "circle",
                    {
                        "cx": f"{lb.x + 16:.2f}",
                        "cy": f"{_flip_y(y + 4, page_h):.2f}",
                        "r": "3.2",
                        "fill": str(getattr(item.symbol, "fill_color", "#C0392B")),
                        "stroke": str(getattr(item.symbol, "stroke_color", "#FFFFFF")),
                    },
                )
            else:
                SubElement(
                    root,
                    "rect",
                    {
                        "x": f"{lb.x + 8:.2f}",
                        "y": f"{_flip_y(y + 8, page_h):.2f}",
                        "width": "16",
                        "height": "8",
                        "fill": str(getattr(item.symbol, "fill_color", "#D9E8F5")),
                        "fill-opacity": str(getattr(item.symbol, "fill_opacity", 0.55)),
                        "stroke": str(getattr(item.symbol, "stroke_color", "#1F4E79")),
                    },
                )
            _text(
                root,
                lb.x + 30,
                y + 1,
                page_h,
                str(item.label)[:28],
                fill="#000000",
                **{"font-family": "Helvetica, Arial, sans-serif", "font-size": "7"},
            )
            y -= 16

    # Norte (flecha + N, equivalente al PDF)
    if layout.north_enabled and layout.north.height > 0 and layout.north.width > 0:
        nb = layout.north
        nx = nb.x + nb.width / 2
        tip_y = nb.y2 - 4
        base_y = nb.y + 10
        arrow_pts = (
            f"{nx:.2f},{_flip_y(tip_y, page_h):.2f} "
            f"{nx - 7:.2f},{_flip_y(base_y, page_h):.2f} "
            f"{nx:.2f},{_flip_y(base_y + 6, page_h):.2f} "
            f"{nx + 7:.2f},{_flip_y(base_y, page_h):.2f}"
        )
        SubElement(
            root,
            "polygon",
            {
                "points": arrow_pts,
                "fill": "#000000",
                "stroke": "none",
            },
        )
        _text(
            root,
            nx,
            tip_y + 2,
            page_h,
            "N",
            fill="#000000",
            **{
                "font-family": "Helvetica, Arial, sans-serif",
                "font-size": "8",
                "font-weight": "bold",
                "text-anchor": "middle",
            },
        )

    # Escala textual
    if layout.scale_bar_enabled and layout.scale_bar.height > 0:
        _text(
            root,
            layout.scale_bar.x,
            layout.scale_bar.y + 10,
            page_h,
            f"1:{int(round(scale_value)):,}".replace(",", " ") if scale_value else "Escala n/d",
            fill="#000000",
            **{"font-family": "Helvetica, Arial, sans-serif", "font-size": "7"},
        )

    _text(
        root,
        layout.footer.x,
        layout.footer.y + layout.footer.height * 0.45,
        page_h,
        footer[:150],
        fill="#333333",
        **{"font-family": "Helvetica, Arial, sans-serif", "font-size": "7.5"},
    )
    _text(
        root,
        layout.footer.x2,
        layout.footer.y + layout.footer.height * 0.45,
        page_h,
        "Cartografía vectorial · no es captura del visor",
        fill="#667788",
        **{
            "font-family": "Helvetica, Arial, sans-serif",
            "font-size": "6.5",
            "text-anchor": "end",
        },
    )

    xml = tostring(root, encoding="utf-8", xml_declaration=True)
    return xml
