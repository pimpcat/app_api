"""PLU multipágina (Fase 3): grid de cartas + índice de armado N de M.

Default del producto: 1 hoja (overview). Multipágina de detalle es opt-in
vía ``params.multipage=true`` en localidades urbanas (escala fija ~1:7 500).
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from shapely.geometry import box as shapely_box
from shapely.geometry.base import BaseGeometry

# Metros por punto de página a escala 1:1 (ReportLab: 72 pt = 1 in)
_METERS_PER_PAGE_POINT = 0.0254 / 72.0

PLU_DETAIL_SCALE = 7500.0
PLU_MAX_PAGES = 40

# Tipografía solo cartas de detalle (no toca overview PLU ni PLR)
PLU_DETAIL_MZA_SIZE = 4.8          # overview ~2.6
PLU_DETAIL_AGEB_SIZE = 9.6         # overview ~2.4 → ×4
PLU_DETAIL_VIAL_SIZE = 2.7         # un poco más pequeña (antes 3.1)
PLU_DETAIL_VIAL_MAX = 2500         # por hoja
# Umbrales (m): 1 etiqueta al centro; 2 si es larga; máx. 3 si es muy larga
PLU_DETAIL_VIAL_LEN_2 = 280.0
PLU_DETAIL_VIAL_LEN_3 = 520.0


def multipage_enabled_for_template(template: dict[str, Any]) -> bool:
    return bool(template.get("multipage", False))


def parse_multipage_param(params: Optional[dict[str, Any]]) -> bool:
    """True si params.multipage pide cartas de detalle."""
    if not params:
        return False
    raw = params.get("multipage")
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on", "si", "sí")
    return bool(raw)


def parse_assembly_package_param(params: Optional[dict[str, Any]]) -> bool:
    """True si se pide hoja índice plotter 90×120 + cartas (paquete).

    Acepta ``params.package == "index_plotter"`` o ``params.assembly_sheet`` truthy.
    """
    if not params:
        return False
    pkg = params.get("package")
    if isinstance(pkg, str) and pkg.strip().lower() in (
        "index_plotter",
        "plotter",
        "assembly",
        "paquete",
    ):
        return True
    if pkg is True:
        return True
    raw = params.get("assembly_sheet")
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on", "si", "sí")
    return bool(raw)


def want_plu_multipage(
    *,
    is_urban: bool,
    params: Optional[dict[str, Any]],
    template: dict[str, Any],
) -> bool:
    """Opt-in: param o flag de plantilla; solo urbano."""
    requested = parse_multipage_param(params) or multipage_enabled_for_template(template)
    return bool(is_urban and requested)


def tile_world_size(
    frame_width_pt: float,
    frame_height_pt: float,
    *,
    target_scale: float = PLU_DETAIL_SCALE,
) -> tuple[float, float]:
    """Ancho/alto en metros del mundo que caben en el map_frame a target_scale."""
    scale = max(float(target_scale or PLU_DETAIL_SCALE), 1.0)
    w = max(float(frame_width_pt), 1.0) * scale * _METERS_PER_PAGE_POINT
    h = max(float(frame_height_pt), 1.0) * scale * _METERS_PER_PAGE_POINT
    return w, h


def plan_plu_tile_grid(
    extent: BaseGeometry,
    *,
    target_scale: float = PLU_DETAIL_SCALE,
    page_width_m: Optional[float] = None,
    page_height_m: Optional[float] = None,
    frame_width_pt: Optional[float] = None,
    frame_height_pt: Optional[float] = None,
    overlap_ratio: float = 0.05,
    mask: Optional[BaseGeometry] = None,
    max_pages: int = PLU_MAX_PAGES,
) -> list[dict[str, Any]]:
    """
    Grilla regular sobre bbox de ``extent`` a escala objetivo.

    Omite tiles sin intersección con ``mask`` (o ``extent``).
    Devuelve ``[{index, total, bounds, row, col}, ...]`` (index 1-based).
    """
    if extent is None or getattr(extent, "is_empty", True):
        return []

    if page_width_m is not None and page_height_m is not None:
        tile_w = max(float(page_width_m), 1.0)
        tile_h = max(float(page_height_m), 1.0)
    elif frame_width_pt is not None and frame_height_pt is not None:
        tile_w, tile_h = tile_world_size(
            float(frame_width_pt),
            float(frame_height_pt),
            target_scale=target_scale,
        )
    else:
        # Compat tests antiguos: tamaño fijo razonable ~ hoja D-Carta a 1:7500
        tile_w, tile_h = 800.0, 560.0

    overlap = max(0.0, min(float(overlap_ratio or 0.0), 0.45))
    step_x = tile_w * (1.0 - overlap)
    step_y = tile_h * (1.0 - overlap)
    if step_x <= 0 or step_y <= 0:
        step_x, step_y = tile_w, tile_h

    minx, miny, maxx, maxy = extent.bounds
    # Asegurar cobertura completa del bbox
    width = max(maxx - minx, 1.0)
    height = max(maxy - miny, 1.0)

    ncols = max(1, int((width - 1e-6) / step_x) + 1)
    nrows = max(1, int((height - 1e-6) / step_y) + 1)

    # Si el extent cabe en una sola carta, una sola hoja
    if width <= tile_w * 1.02 and height <= tile_h * 1.02:
        ncols, nrows = 1, 1
        # Centrar el tile sobre el extent
        cx = (minx + maxx) / 2.0
        cy = (miny + maxy) / 2.0
        origin_x = cx - tile_w / 2.0
        origin_y = cy - tile_h / 2.0
    else:
        origin_x = minx
        origin_y = miny
        # Extender origen para cubrir max con steps enteros
        cover_w = step_x * (ncols - 1) + tile_w
        cover_h = step_y * (nrows - 1) + tile_h
        if cover_w < width:
            ncols += 1
            cover_w = step_x * (ncols - 1) + tile_w
        if cover_h < height:
            nrows += 1
            cover_h = step_y * (nrows - 1) + tile_h
        # Centrar sobrante
        origin_x = minx - max(0.0, (cover_w - width) / 2.0)
        origin_y = miny - max(0.0, (cover_h - height) / 2.0)

    hit_mask = mask if mask is not None and not getattr(mask, "is_empty", True) else extent
    lim = max(1, min(int(max_pages or PLU_MAX_PAGES), 200))

    candidates: list[dict[str, Any]] = []
    for row in range(nrows):
        for col in range(ncols):
            x0 = origin_x + col * step_x
            y0 = origin_y + row * step_y
            x1 = x0 + tile_w
            y1 = y0 + tile_h
            tile = shapely_box(x0, y0, x1, y1)
            try:
                if not tile.intersects(hit_mask):
                    continue
            except Exception:
                continue
            candidates.append(
                {
                    "bounds": (float(x0), float(y0), float(x1), float(y1)),
                    "row": int(row),
                    "col": int(col),
                }
            )

    if not candidates:
        # Fallback: un tile centrado en extent
        cx = (minx + maxx) / 2.0
        cy = (miny + maxy) / 2.0
        candidates = [
            {
                "bounds": (
                    cx - tile_w / 2.0,
                    cy - tile_h / 2.0,
                    cx + tile_w / 2.0,
                    cy + tile_h / 2.0,
                ),
                "row": 0,
                "col": 0,
            }
        ]

    # Orden: fila de arriba (N) a abajo, izquierda a derecha
    candidates.sort(key=lambda t: (-t["row"], t["col"]))
    if len(candidates) > lim:
        candidates = candidates[:lim]

    total = len(candidates)
    out: list[dict[str, Any]] = []
    for i, t in enumerate(candidates):
        out.append(
            {
                "index": i + 1,
                "total": total,
                "bounds": t["bounds"],
                "row": t["row"],
                "col": t["col"],
            }
        )
    return out


def filter_features_to_bounds(
    features: Sequence[dict[str, Any]] | None,
    bounds: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Conserva puntos/etiquetas cuya geometría intersecta el tile."""
    if not features:
        return []
    tile = shapely_box(*bounds)
    kept: list[dict[str, Any]] = []
    for feat in features:
        geom = feat.get("geometry") if isinstance(feat, dict) else None
        if geom is None or getattr(geom, "is_empty", True):
            continue
        try:
            if tile.intersects(geom):
                kept.append(feat)
        except Exception:
            continue
    return kept


def _format_vialidad_tipo_nom(lab: dict[str, Any]) -> str:
    """Formato tipovial + nomvial (espacio). Sin cvevial."""
    from cartography_engine.datasource import format_vialidad_tipo_nom

    return format_vialidad_tipo_nom(
        nomvial=str(lab.get("nomvial") or ""),
        tipovial=str(lab.get("tipovial") or ""),
        text=str(lab.get("text") or ""),
    )


def vialidad_labels_in_bounds(
    labels: Sequence[dict[str, Any]] | None,
    bounds: tuple[float, float, float, float],
    *,
    size: float = 3.8,
    max_labels: int = 220,
    skip_ninguno: bool = True,
    spacing_m: float = 0.0,  # legacy; ignorado — usamos 1..3 por longitud
    text_mode: str = "tipo_nom",
) -> list[dict[str, Any]]:
    """
    Etiquetas de calle por hoja (insumo independiente).

    - Un nombre al centro del trazo visible en el tile.
    - Si la vialidad es larga en la hoja: 2; si es muy larga: máx. 3.
    - text_mode ``tipo_nom`` → tipovial + nomvial (solo multipágina).
    """
    if not labels:
        return []
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import linemerge

    from cartography_engine.datasource import _line_midpoint_angle

    tile = shapely_box(*bounds)
    try:
        hit = tile.buffer(15.0)
    except Exception:
        hit = tile

    def _as_line(geom: Any) -> Any:
        if geom is None or getattr(geom, "is_empty", True):
            return None
        if isinstance(geom, LineString):
            return geom
        try:
            merged = linemerge(geom)
        except Exception:
            merged = geom
        if isinstance(merged, LineString) and merged.length > 0:
            return merged
        if isinstance(merged, MultiLineString):
            parts = [p for p in merged.geoms if isinstance(p, LineString) and p.length > 0]
            if not parts:
                return None
            return max(parts, key=lambda p: p.length)
        if hasattr(geom, "geoms"):
            parts = []
            for g in geom.geoms:
                ln = _as_line(g)
                if ln is not None:
                    parts.append(ln)
            if not parts:
                return None
            return max(parts, key=lambda p: p.length)
        return None

    def _n_labels(length_m: float) -> int:
        if length_m < 12.0:
            return 0
        if length_m >= PLU_DETAIL_VIAL_LEN_3:
            return 3
        if length_m >= PLU_DETAIL_VIAL_LEN_2:
            return 2
        return 1

    def _placements(line: Any, n: int) -> list[tuple[Any, float]]:
        if line is None or n <= 0:
            return []
        if n == 1:
            mid, ang = _line_midpoint_angle(line)
            return [(mid, ang)] if mid is not None else []
        out_pts: list[tuple[Any, float]] = []
        import math

        for i in range(n):
            t = (i + 0.5) / n
            try:
                pt = line.interpolate(t, normalized=True)
                t0 = max(0.0, t - 0.06)
                t1 = min(1.0, t + 0.06)
                p0 = line.interpolate(t0, normalized=True)
                p1 = line.interpolate(t1, normalized=True)
                dx, dy = p1.x - p0.x, p1.y - p0.y
                ang = 0.0
                if abs(dx) > 1e-9 or abs(dy) > 1e-9:
                    ang = math.degrees(math.atan2(dy, dx))
                    if ang > 90.0:
                        ang -= 180.0
                    elif ang < -90.0:
                        ang += 180.0
                out_pts.append((pt, float(ang)))
            except Exception:
                continue
        return out_pts

    out: list[dict[str, Any]] = []
    for lab in labels:
        if str(lab.get("layer_id") or "") != "ejes":
            continue
        if text_mode == "tipo_nom":
            text = _format_vialidad_tipo_nom(lab)
        else:
            text = str(lab.get("text") or "").strip()
        if not text:
            continue
        if skip_ninguno and "NINGUNO" in text.upper():
            continue
        line = lab.get("_line")
        try:
            if line is not None and not getattr(line, "is_empty", True):
                if not hit.intersects(line):
                    continue
                clipped = line.intersection(tile)
                if clipped is None or getattr(clipped, "is_empty", True):
                    continue
                length = float(getattr(clipped, "length", 0.0) or 0.0)
                n = _n_labels(length)
                if n <= 0:
                    continue
                # Una sola geometría de referencia para la calle en esta hoja
                primary = _as_line(clipped)
                if primary is None:
                    continue
                for mid, angle in _placements(primary, n):
                    if mid is None:
                        continue
                    out.append(
                        {
                            "text": text,
                            "geometry": mid,
                            "layer_id": "ejes",
                            "color": lab.get("color") or "#1a1a1a",
                            "bold": False,
                            "size": float(size or lab.get("size") or 3.8),
                            "style": "along",
                            "anchor": "center",
                            "angle": float(angle or 0.0),
                            "offset": 0.0,
                        }
                    )
            else:
                geom = lab.get("geometry")
                if geom is None or getattr(geom, "is_empty", True):
                    continue
                if not tile.intersects(geom):
                    continue
                out.append(
                    {
                        "text": text,
                        "geometry": geom,
                        "layer_id": "ejes",
                        "color": lab.get("color") or "#1a1a1a",
                        "bold": False,
                        "size": float(size or lab.get("size") or 3.8),
                        "style": "along",
                        "anchor": "center",
                        "angle": float(lab.get("angle") or 0.0),
                        "offset": 0.0,
                    }
                )
        except Exception:
            continue

    out.sort(key=lambda it: len(str(it.get("text") or "")))
    return out[: max(1, min(int(max_labels), 5000))]


def style_plu_detail_page_labels(
    labels: Sequence[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    Escalas tipográficas solo para cartas multipágina.
    No mutar plantilla overview: trabajar sobre copia por hoja.
    """
    if not labels:
        return []
    out: list[dict[str, Any]] = []
    for lab in labels:
        item = dict(lab)
        lid = str(item.get("layer_id") or "")
        style = str(item.get("style") or "")
        if lid in ("manzanas", "manzana"):
            item["size"] = PLU_DETAIL_MZA_SIZE
            item["bold"] = False
        elif lid == "ageb" or (style == "ageb_oval" and lid != "colindantes"):
            item["size"] = PLU_DETAIL_AGEB_SIZE
            item["bold"] = False
            item["style"] = "ageb_oval"
        elif lid == "ejes":
            item["size"] = PLU_DETAIL_VIAL_SIZE
            item["bold"] = False
        # colindantes / SIL / resto: se dejan como vienen del overview filtrado
        out.append(item)
    return out


def clip_layers_to_bounds(
    layers: Sequence[Any],
    bounds: tuple[float, float, float, float],
    *,
    buffer_m: float = 40.0,
) -> list[Any]:
    """
    Recorta geometrías de capa al tile (+ buffer).
    Evita redibujar toda la ciudad en cada hoja (causa del 504 ~60s).
    """
    from cartography_engine.layers import LayerData

    try:
        from shapely import clip_by_rect
    except ImportError:  # pragma: no cover
        clip_by_rect = None  # type: ignore

    minx, miny, maxx, maxy = bounds
    pad = max(0.0, float(buffer_m or 0.0))
    x0, y0, x1, y1 = minx - pad, miny - pad, maxx + pad, maxy + pad
    tile = shapely_box(x0, y0, x1, y1)
    out: list[Any] = []
    for ld in layers or []:
        geom = getattr(ld, "geometry", None)
        definition = getattr(ld, "definition", None)
        count = int(getattr(ld, "feature_count", 0) or 0)
        if geom is None or getattr(geom, "is_empty", True):
            out.append(ld)
            continue
        try:
            env = geom.bounds
            if env[2] < x0 or env[0] > x1 or env[3] < y0 or env[1] > y1:
                out.append(
                    LayerData(definition=definition, geometry=None, feature_count=0)
                )
                continue
            if clip_by_rect is not None:
                clipped = clip_by_rect(geom, x0, y0, x1, y1)
            else:
                clipped = geom.intersection(tile)
            if clipped is None or getattr(clipped, "is_empty", True):
                out.append(
                    LayerData(definition=definition, geometry=None, feature_count=0)
                )
            else:
                out.append(
                    LayerData(
                        definition=definition,
                        geometry=clipped,
                        feature_count=count,
                    )
                )
        except Exception:
            out.append(ld)
    return out
