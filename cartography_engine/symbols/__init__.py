"""Símbolos GroSIG mínimos (fill/stroke) — independientes de QGIS/SLD."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class PolygonSymbol:
    fill_color: str = "#D9E8F5"
    fill_opacity: float = 0.55
    stroke_color: str = "#1F4E79"
    stroke_width: float = 1.2
    # Relleno rayado (localidad rural amanzanada)
    hatch: bool = False
    dash: Optional[Tuple[float, ...]] = None


@dataclass(frozen=True)
class LineSymbol:
    stroke_color: str = "#333333"
    stroke_width: float = 0.8
    dash: Optional[Tuple[float, ...]] = None
    # cross = límite estatal (+); rail = vía férrea; double = multi-carril;
    # double_dash = doble (un lado continuo / otro discontinuo)
    decoration: Optional[str] = None


@dataclass(frozen=True)
class PointSymbol:
    fill_color: str = "#C0392B"
    stroke_color: str = "#FFFFFF"
    size: float = 4.0
    # plane = glifo aeropuerto simple
    marker: Optional[str] = None


def polygon_symbol_from_dict(data: Optional[dict[str, Any]]) -> PolygonSymbol:
    d = data or {}
    dash_raw = d.get("dash")
    dash: Optional[Tuple[float, ...]] = None
    if isinstance(dash_raw, (list, tuple)) and dash_raw:
        try:
            dash = tuple(float(x) for x in dash_raw)
        except (TypeError, ValueError):
            dash = None
    return PolygonSymbol(
        fill_color=str(d.get("fill_color", d.get("fill", "#D9E8F5"))),
        fill_opacity=float(d.get("fill_opacity", 0.55)),
        stroke_color=str(d.get("stroke_color", d.get("stroke", "#1F4E79"))),
        stroke_width=float(d.get("stroke_width", d.get("width", 1.2))),
        hatch=bool(d.get("hatch", False)),
        dash=dash,
    )


def line_symbol_from_dict(data: Optional[dict[str, Any]]) -> LineSymbol:
    d = data or {}
    dash_raw = d.get("dash")
    dash: Optional[Tuple[float, ...]] = None
    if isinstance(dash_raw, (list, tuple)) and dash_raw:
        try:
            dash = tuple(float(x) for x in dash_raw)
        except (TypeError, ValueError):
            dash = None
    deco = d.get("decoration") or d.get("line_decoration")
    decoration = str(deco).strip().lower() if deco else None
    if decoration not in (
        "cross",
        "rail",
        "double",
        "double_dash",
        "double_mixed",
        "mixed_double",
        "half_dash",
    ):
        decoration = None
    if decoration in ("double_mixed", "mixed_double", "half_dash"):
        decoration = "double_dash"
    return LineSymbol(
        stroke_color=str(d.get("stroke_color", d.get("stroke", d.get("color", "#333333")))),
        stroke_width=float(d.get("stroke_width", d.get("width", 0.8))),
        dash=dash,
        decoration=decoration,
    )


def point_symbol_from_dict(data: Optional[dict[str, Any]]) -> PointSymbol:
    d = data or {}
    marker_raw = d.get("marker")
    marker = str(marker_raw).strip().lower() if marker_raw else None
    if marker not in ("plane", "triangle", "tri"):
        marker = None
    if marker == "tri":
        marker = "triangle"
    return PointSymbol(
        fill_color=str(d.get("fill_color", d.get("fill", d.get("color", "#C0392B")))),
        stroke_color=str(d.get("stroke_color", d.get("stroke", "#FFFFFF"))),
        size=float(d.get("size", 3.5)),
        marker=marker,
    )
