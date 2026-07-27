"""Layout Engine: composición paramétrica data-driven (presets + JSON)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Optional

from cartography_engine.models import CartographyError

# plotter: cm → pt (1 pt = 1/72 in; 1 in = 2.54 cm)
PAPER_SIZES = {
    "letter": (612.0, 792.0),
    "a4": (595.27, 841.89),
    # 90×60 cm landscape canónico (ancho × alto)
    "plotter_90x60": (2551.18, 1700.79),
    # 90×70 cm landscape canónico (ancho × alto) — croquis municipal
    "plotter_90x70": (2551.18, 1984.25),
    # 90×120 cm portrait canónico (ancho × alto) — condensado
    "plotter_90x120": (2551.18, 3401.57),
    # Doble carta GroSIG 42×28 cm landscape canónico
    "dcarta_42x28": (1190.55, 793.70),
}

PresetName = Literal[
    "default",
    "compact",
    "map_focus",
    "grosig_localidad",
    "grosig_marginalia",
    "grosig_croquis_90x70",
    "grosig_condensado",
]


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.height


@dataclass(frozen=True)
class LayoutSpec:
    """Parámetros de composición (no coordenadas absolutas libres)."""

    preset: str = "default"
    paper: str = "letter"
    orientation: Literal["portrait", "landscape"] = "portrait"
    margin: float = 36.0
    brand_height: float = 36.0
    title_height: float = 34.0
    footer_height: float = 30.0
    chrome_gap: float = 8.0
    legend_enabled: bool = True
    legend_width: float = 118.0
    legend_position: str = "right"
    north_enabled: bool = True
    scale_bar_enabled: bool = True
    show_brand_header: bool = True
    show_outer_frame: bool = True
    strip_enabled: bool = False
    strip_ratio: float = 0.22


@dataclass(frozen=True)
class PageLayout:
    page_width: float
    page_height: float
    margin: float
    outer_frame: Box
    brand_header: Box
    title: Box
    map_frame: Box
    north: Box
    scale_bar: Box
    footer: Box
    legend: Optional[Box] = None
    strip: Optional[Box] = None
    show_brand_header: bool = True
    show_outer_frame: bool = True
    north_enabled: bool = True
    scale_bar_enabled: bool = True
    strip_enabled: bool = False


PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "margin": 36.0,
        "brand_height": 36.0,
        "title_height": 34.0,
        "footer_height": 30.0,
        "chrome_gap": 8.0,
        "legend_width": 118.0,
    },
    "compact": {
        "margin": 24.0,
        "brand_height": 28.0,
        "title_height": 26.0,
        "footer_height": 24.0,
        "chrome_gap": 6.0,
        "legend_width": 100.0,
    },
    "map_focus": {
        "margin": 20.0,
        "brand_height": 26.0,
        "title_height": 22.0,
        "footer_height": 22.0,
        "chrome_gap": 4.0,
        "legend_width": 96.0,
        "orientation": "landscape",
    },
    "grosig_localidad": {
        "paper": "dcarta_42x28",
        "orientation": "landscape",
        "margin": 12.0,
        "brand_height": 0.0,
        "title_height": 0.0,
        "footer_height": 0.0,
        "chrome_gap": 3.0,
        "legend_enabled": False,
        "north_enabled": False,
        "scale_bar_enabled": False,
        "show_brand_header": False,
        "strip_enabled": True,
        "strip_ratio": 0.10,
    },
    "grosig_marginalia": {
        "paper": "plotter_90x60",
        "orientation": "landscape",
        "margin": 24.0,
        "brand_height": 48.0,
        "title_height": 36.0,
        "footer_height": 32.0,
        "chrome_gap": 8.0,
        "legend_width": 210.0,
        "legend_enabled": True,
        "orientation_force": "landscape",
    },
    # Croquis municipal: mapa + panel derecho completo (sin cabecera/tira)
    "grosig_croquis_90x70": {
        "paper": "plotter_90x70",
        "orientation": "landscape",
        "margin": 20.0,
        "brand_height": 0.0,
        "title_height": 0.0,
        "footer_height": 0.0,
        "chrome_gap": 6.0,
        "legend_width": 260.0,
        "legend_enabled": True,
        "north_enabled": False,
        "scale_bar_enabled": False,
        "show_brand_header": False,
        "orientation_force": "landscape",
    },
    "grosig_condensado": {
        "paper": "plotter_90x120",
        "orientation": "landscape",
        "margin": 28.0,
        "brand_height": 0.0,
        "title_height": 0.0,
        "footer_height": 0.0,
        "chrome_gap": 10.0,
        "legend_width": 340.0,
        "legend_enabled": True,
        "north_enabled": False,
        "scale_bar_enabled": False,
        "show_brand_header": False,
    },
}

def page_size(
    paper: str = "letter",
    orientation: Literal["portrait", "landscape"] = "portrait",
) -> tuple[float, float]:
    w, h = PAPER_SIZES.get(paper, PAPER_SIZES["letter"])
    # plotter_90x60 / 90x70 y dcarta_42x28 se guardan en landscape; plotter_90x120 en portrait
    if paper in ("plotter_90x60", "plotter_90x70", "dcarta_42x28"):
        if orientation == "portrait":
            return h, w
        return w, h
    if paper == "plotter_90x120":
        if orientation == "landscape":
            return h, w
        return w, h
    if orientation == "landscape":
        return h, w
    return w, h


def _clamp_float(name: str, value: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v < lo or v > hi:
        raise CartographyError(
            "INVALID_LAYOUT",
            f"layout.{name} fuera de rango [{lo}, {hi}]: {v}",
        )
    return v


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "si", "sí"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)  # type: ignore[arg-type]
        else:
            out[key] = val
    return out


def parse_layout_spec(
    raw: Optional[dict[str, Any]] = None,
    *,
    paper: Optional[str] = None,
    orientation: Optional[str] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> LayoutSpec:
    """
    Resuelve LayoutSpec: preset → plantilla → overrides (params.layout).
    ``paper`` / ``orientation`` del request API tienen prioridad final si se pasan.
    """
    data: dict[str, Any] = {}
    if isinstance(raw, dict):
        data = dict(raw)
    if isinstance(overrides, dict):
        data = _deep_merge(data, overrides)

    preset_name = str(data.get("preset") or "default").strip().lower()
    if preset_name not in PRESETS:
        raise CartographyError(
            "INVALID_LAYOUT",
            f"layout.preset desconocido: {preset_name} "
            "(use default|compact|map_focus|grosig_localidad|grosig_marginalia|"
            "grosig_croquis_90x70|grosig_condensado)",
        )

    merged: dict[str, Any] = dict(PRESETS[preset_name])
    for key in (
        "paper",
        "orientation",
        "margin",
        "brand_height",
        "title_height",
        "footer_height",
        "chrome_gap",
        "show_brand_header",
        "show_outer_frame",
        "strip_enabled",
        "strip_ratio",
    ):
        if key in data and data[key] is not None:
            merged[key] = data[key]

    legend_raw = data.get("legend") if isinstance(data.get("legend"), dict) else {}
    north_raw = data.get("north") if isinstance(data.get("north"), dict) else {}
    scale_raw = data.get("scale_bar") if isinstance(data.get("scale_bar"), dict) else {}
    strip_raw = data.get("strip") if isinstance(data.get("strip"), dict) else {}

    if "enabled" in legend_raw:
        merged["legend_enabled"] = legend_raw["enabled"]
    elif "legend_enabled" in data:
        merged["legend_enabled"] = data["legend_enabled"]
    else:
        merged.setdefault("legend_enabled", True)

    if "width" in legend_raw:
        merged["legend_width"] = legend_raw["width"]
    if "position" in legend_raw:
        merged["legend_position"] = legend_raw["position"]
    else:
        merged.setdefault("legend_position", "right")

    if "enabled" in north_raw:
        merged["north_enabled"] = north_raw["enabled"]
    else:
        merged.setdefault("north_enabled", True)

    if "enabled" in scale_raw:
        merged["scale_bar_enabled"] = scale_raw["enabled"]
    else:
        merged.setdefault("scale_bar_enabled", True)

    if "enabled" in strip_raw:
        merged["strip_enabled"] = strip_raw["enabled"]
    else:
        merged.setdefault("strip_enabled", False)
    if "ratio" in strip_raw:
        merged["strip_ratio"] = strip_raw["ratio"]

    pap = str(merged.get("paper") or "letter").strip().lower()
    ori = str(merged.get("orientation") or "portrait").strip().lower()
    if paper:
        pap = str(paper).strip().lower()
    if orientation:
        ori = str(orientation).strip().lower()
    if pap not in PAPER_SIZES:
        raise CartographyError("INVALID_LAYOUT", f"layout.paper inválido: {pap}")
    if ori not in ("portrait", "landscape"):
        raise CartographyError("INVALID_LAYOUT", f"layout.orientation inválida: {ori}")

    legend_pos = str(merged.get("legend_position") or "right").strip().lower()
    if legend_pos != "right":
        raise CartographyError(
            "INVALID_LAYOUT",
            "layout.legend.position solo admite 'right'",
        )

    _is_loc_strip = preset_name == "grosig_localidad"
    brand_default = 0.0 if _is_loc_strip else 36.0
    title_default = 0.0 if _is_loc_strip else 34.0
    footer_default = 0.0 if _is_loc_strip else 30.0

    return LayoutSpec(
        preset=preset_name,
        paper=pap,
        orientation=ori,  # type: ignore[arg-type]
        margin=_clamp_float("margin", merged.get("margin"), 8.0, 72.0, 36.0),
        brand_height=_clamp_float(
            "brand_height", merged.get("brand_height"), 0.0, 80.0, brand_default
        ),
        title_height=_clamp_float(
            "title_height", merged.get("title_height"), 0.0, 56.0, title_default
        ),
        footer_height=_clamp_float(
            "footer_height", merged.get("footer_height"), 0.0, 48.0, footer_default
        ),
        chrome_gap=_clamp_float("chrome_gap", merged.get("chrome_gap"), 2.0, 24.0, 8.0),
        legend_enabled=_as_bool(merged.get("legend_enabled"), True),
        legend_width=_clamp_float("legend.width", merged.get("legend_width"), 80.0, 480.0, 118.0),
        legend_position=legend_pos,
        north_enabled=_as_bool(merged.get("north_enabled"), True),
        scale_bar_enabled=_as_bool(merged.get("scale_bar_enabled"), True),
        show_brand_header=_as_bool(merged.get("show_brand_header"), True),
        show_outer_frame=_as_bool(merged.get("show_outer_frame"), True),
        strip_enabled=_as_bool(merged.get("strip_enabled"), False),
        strip_ratio=_clamp_float("strip.ratio", merged.get("strip_ratio"), 0.08, 0.35, 0.22),
    )


def resolve_layout_spec(
    template: dict[str, Any],
    *,
    paper: Optional[str] = None,
    orientation: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    prefer_template_page: bool = False,
) -> LayoutSpec:
    """Plantilla.layout + params.layout; paper/orientation del request solo si se pasan."""
    raw = template.get("layout") if isinstance(template.get("layout"), dict) else {}
    overrides = (params or {}).get("layout") if isinstance((params or {}).get("layout"), dict) else None
    if prefer_template_page:
        return parse_layout_spec(raw, paper=None, orientation=None, overrides=overrides)
    return parse_layout_spec(
        raw,
        paper=paper,
        orientation=orientation,
        overrides=overrides,
    )


def build_layout(
    paper: str = "letter",
    orientation: Literal["portrait", "landscape"] = "portrait",
    margin: float = 36.0,
    legend_items: int = 0,
    spec: Optional[LayoutSpec] = None,
) -> PageLayout:
    """Composición institucional a partir de LayoutSpec (o defaults legacy)."""
    if spec is None:
        spec = LayoutSpec(paper=paper, orientation=orientation, margin=margin)
    else:
        if paper and paper != spec.paper:
            spec = replace(spec, paper=paper)
        if orientation and orientation != spec.orientation:
            spec = replace(spec, orientation=orientation)  # type: ignore[arg-type]

    page_w, page_h = page_size(spec.paper, spec.orientation)
    m = float(spec.margin)

    outer = Box(m, m, page_w - 2 * m, page_h - 2 * m)

    # --- Plano localidad: mapa arriba + tira abajo ---
    if spec.strip_enabled:
        strip_h = max(70.0, outer.height * float(spec.strip_ratio))
        strip = Box(outer.x, outer.y, outer.width, strip_h)
        map_frame = Box(
            outer.x + 6,
            strip.y2 + 6,
            outer.width - 12,
            max(120.0, outer.y2 - strip.y2 - 12),
        )
        empty = Box(0, 0, 0, 0)
        return PageLayout(
            page_width=page_w,
            page_height=page_h,
            margin=m,
            outer_frame=outer,
            brand_header=empty,
            title=empty,
            map_frame=map_frame,
            north=empty,
            scale_bar=empty,
            footer=empty,
            legend=None,
            strip=strip,
            show_brand_header=False,
            show_outer_frame=spec.show_outer_frame,
            north_enabled=False,
            scale_bar_enabled=False,
            strip_enabled=True,
        )

    brand_h = float(spec.brand_height) if spec.show_brand_header else 0.0
    title_h = float(spec.title_height)
    footer_h = float(spec.footer_height)
    chrome_gap = float(spec.chrome_gap)

    if brand_h > 0:
        brand_header = Box(outer.x + 6, outer.y2 - brand_h - 4, outer.width - 12, brand_h)
        title_top = brand_header.y
    else:
        brand_header = Box(outer.x + 6, outer.y2 - 4, outer.width - 12, 0.0)
        title_top = outer.y2 - 4

    title = Box(outer.x + 8, title_top - title_h - 4, outer.width - 16, title_h)

    chrome_bottom = 0.0
    if spec.north_enabled or spec.scale_bar_enabled:
        # Más alto en plotter para tipografía legible
        chrome_bottom = 64.0 if page_w >= 1400 else 48.0

    map_top = title.y - chrome_gap if title_h > 0 else outer.y2 - chrome_gap
    map_bottom = outer.y + footer_h + chrome_bottom

    legend_box: Optional[Box] = None
    legend_w = 0.0
    use_legend = spec.legend_enabled and legend_items > 0
    if use_legend:
        legend_w = float(spec.legend_width)
        # Columna completa (estilo GroSIG): evita el vacío bajo una leyenda enana
        legend_h = max(80.0, map_top - map_bottom)
        legend_box = Box(
            outer.x2 - legend_w - 8,
            map_bottom,
            legend_w,
            legend_h,
        )

    map_right_pad = (legend_w + 16.0) if legend_box else 10.0
    map_frame = Box(
        outer.x + 10,
        map_bottom,
        max(80.0, outer.width - 10 - map_right_pad),
        max(80.0, map_top - map_bottom),
    )

    north_size = 52.0 if page_w >= 1400 else 40.0
    if spec.north_enabled:
        north = Box(map_frame.x2 - north_size - 8, map_frame.y - chrome_bottom + 8, north_size, north_size)
    else:
        north = Box(map_frame.x2 - 48, map_frame.y - 4, 0.0, 0.0)

    scale_w = min(260.0, map_frame.width * 0.42) if page_w >= 1400 else min(200.0, map_frame.width * 0.45)
    scale_h = 40.0 if page_w >= 1400 else 30.0
    if spec.scale_bar_enabled:
        scale_bar = Box(map_frame.x, map_frame.y - chrome_bottom + 10, scale_w, scale_h)
    else:
        scale_bar = Box(map_frame.x, map_frame.y - 4, 0.0, 0.0)

    footer = Box(outer.x + 8, outer.y + 4, outer.width - 16, max(footer_h, 0.0))

    return PageLayout(
        page_width=page_w,
        page_height=page_h,
        margin=m,
        outer_frame=outer,
        brand_header=brand_header,
        title=title,
        map_frame=map_frame,
        north=north,
        scale_bar=scale_bar,
        footer=footer,
        legend=legend_box,
        strip=None,
        show_brand_header=spec.show_brand_header,
        show_outer_frame=spec.show_outer_frame,
        north_enabled=spec.north_enabled,
        scale_bar_enabled=spec.scale_bar_enabled,
        strip_enabled=False,
    )
