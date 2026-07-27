"""Colisión de etiquetas (1.1): priorización + cajas en espacio de página."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from cartography_engine.layouts import Box
from cartography_engine.renderers import world_to_page


@dataclass(frozen=True)
class PlacedLabel:
    text: str
    x: float  # page coords (ReportLab: origen abajo-izquierda)
    y: float
    width: float
    height: float
    layer_id: str = ""
    color: str = "#1a1a1a"
    bold: bool = False
    italic: bool = False
    font_size: float = 7.5
    style: str = ""
    angle: float = 0.0
    offset: float = 0.0


@dataclass(frozen=True)
class _Candidate:
    text: str
    ax: float
    ay: float
    width: float
    height: float
    priority: float
    layer_id: str
    color: str
    bold: bool
    italic: bool
    font_size: float
    style: str
    anchor: str
    angle: float = 0.0
    offset: float = 0.0


def _point_xy(geom: BaseGeometry) -> Optional[tuple[float, float]]:
    if geom is None or getattr(geom, "is_empty", False):
        return None
    if isinstance(geom, Point):
        return float(geom.x), float(geom.y)
    try:
        pt = geom.representative_point()
        return float(pt.x), float(pt.y)
    except Exception:
        return None


def _estimate_text_size(text: str, font_size: float = 6.5) -> tuple[float, float]:
    # Aproximación Helvetica: ~0.5em por carácter (soporta multilínea).
    lines = str(text or "").split("\n") or [""]
    max_len = max((len(ln) for ln in lines), default=0)
    w = max(8.0, max_len * font_size * 0.52)
    h = font_size * 1.25 * max(1, len(lines))
    return w, h


def _estimate_along_collision_size(
    text: str, font_size: float, angle: float = 0.0
) -> tuple[float, float]:
    """
    Caja de colisión más compacta para texto rotado sobre el eje.
    Evita que el AABB del texto vertical 'expulse' la etiqueta de la calle.
    """
    import math

    tw, th = _estimate_text_size(text, font_size)
    # Huella aproximada proyectada (más estrecha transversal al trazo)
    rad = math.radians(angle)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    # Ancho a lo largo del texto; alto transversal reducido
    along = tw * 0.72
    across = max(font_size * 0.95, th * 0.55)
    w = along * c + across * s
    h = along * s + across * c
    return max(6.0, w), max(font_size * 0.9, h)


def _boxes_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    pad: float,
) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (
        ax + aw + pad <= bx
        or bx + bw + pad <= ax
        or ay + ah + pad <= by
        or by + bh + pad <= ay
    )


def _inside_frame(
    box: tuple[float, float, float, float], frame: Box, margin: float = 2.0
) -> bool:
    x, y, w, h = box
    return (
        x >= frame.x + margin
        and y >= frame.y + margin
        and x + w <= frame.x2 - margin
        and y + h <= frame.y2 - margin
    )


def _box_for_anchor(
    ax: float,
    ay: float,
    tw: float,
    th: float,
    ox: float,
    oy: float,
    anchor: str,
) -> tuple[float, float, float, float]:
    """Caja de texto. anchor=center → centro del texto en (ax,ay) + nudge."""
    if anchor == "center":
        return (ax - tw / 2.0 + ox, ay - th / 2.0 + oy, tw, th)
    x = ax + ox
    if ox < 0:
        x = ax - tw - abs(ox)
    return (x, ay + oy, tw, th)


def _overlap_count(
    box: tuple[float, float, float, float],
    occupied: Sequence[tuple[float, float, float, float]],
    pad: float,
) -> int:
    return sum(1 for other in occupied if _boxes_overlap(box, other, pad))


def _pick_best_label_box(
    *,
    cand: _Candidate,
    offsets: Sequence[tuple[float, float]],
    frame: Box,
    occupied: Sequence[tuple[float, float, float, float]],
    padding: float,
    extra_obstacles: Sequence[tuple[float, float, float, float]] = (),
    margin: float = 2.0,
    max_score: int = 10**9,
) -> Optional[tuple[float, float, float, float]]:
    """Elige el offset con menos solapes; prefiere score 0 y el orden de ``offsets``.

    ``max_score``: descarta posiciones con más solapes que este tope (0 = hueco libre).
    """
    occ = list(occupied) + list(extra_obstacles)
    best: Optional[tuple[float, float, float, float]] = None
    best_score = 10**9
    for ox, oy in offsets:
        box = _box_for_anchor(
            cand.ax, cand.ay, cand.width, cand.height, ox, oy, cand.anchor
        )
        if not _inside_frame(box, frame, margin=margin):
            continue
        score = _overlap_count(box, occ, padding)
        if score > int(max_score):
            continue
        if score < best_score:
            best_score = score
            best = box
            if score == 0:
                break
    return best


def _point_label_offsets(fs: float) -> list[tuple[float, float]]:
    """Anillos de colocación alrededor del punto (nunca encima del símbolo)."""
    # Radios en puntos de página; escalan un poco con el tamaño de fuente.
    k = max(0.85, min(1.25, float(fs) / 5.2))
    rings = (7.0, 10.0, 13.0, 17.0, 21.0, 26.0, 32.0)
    angles_deg = (
        90,
        60,
        120,
        30,
        150,
        0,
        180,
        -30,
        -150,
        -60,
        -120,
        -90,
        45,
        135,
        -45,
        -135,
    )
    import math

    out: list[tuple[float, float]] = []
    seen: set[tuple[int, int]] = set()
    for r in rings:
        rr = r * k
        for deg in angles_deg:
            rad = math.radians(float(deg))
            ox = rr * math.cos(rad)
            oy = rr * math.sin(rad)
            key = (int(round(ox)), int(round(oy)))
            if key in seen:
                continue
            seen.add(key)
            out.append((ox, oy))
    return out


def resolve_label_collisions(
    labels: Sequence[dict[str, Any]],
    *,
    bounds: tuple[float, float, float, float],
    frame: Box,
    font_size: float = 6.5,
    padding: float = 2.0,
    max_labels: int = 60,
    obstacles: Optional[Sequence[tuple[float, float, float, float]]] = None,
) -> list[PlacedLabel]:
    """
    Coloca etiquetas evitando solapes.
    Prioriza textos más cortos y puntos más cercanos al centro del bbox.
    `obstacles`: cajas en página (SIP, CD, etc.) ya ocupadas.
    """
    if not labels or not bounds:
        return []

    minx, miny, maxx, maxy = bounds
    cx = (minx + maxx) / 2.0
    cy = (miny + maxy) / 2.0

    # Pre-scan: puntos de localidad (aisladas primero en el orden de colocación).
    point_page_xy: list[tuple[float, float]] = []
    for item in labels:
        if str(item.get("layer_id") or "") not in ("localidades_p", "ctx_localidades_p"):
            continue
        geom0 = item.get("geometry")
        xy0 = _point_xy(geom0) if geom0 is not None else None
        if xy0:
            point_page_xy.append(world_to_page(xy0[0], xy0[1], bounds, frame))

    candidates: list[_Candidate] = []
    for idx, item in enumerate(labels):
        text = str(item.get("text") or "").strip()
        geom = item.get("geometry")
        if not text or geom is None:
            continue
        text = text[:48]
        xy = _point_xy(geom)
        if not xy:
            continue
        wx, wy = xy
        px, py = world_to_page(wx, wy, bounds, frame)
        try:
            item_fs = float(item.get("size") or 0)
        except (TypeError, ValueError):
            item_fs = 0.0
        fs = item_fs if item_fs > 0 else font_size
        style = str(item.get("style") or "").strip().lower()
        anchor = str(item.get("anchor") or "auto").strip().lower()
        layer_id = str(item.get("layer_id") or "")
        try:
            angle = float(item.get("angle") or 0.0)
        except (TypeError, ValueError):
            angle = 0.0
        try:
            offset = float(item.get("offset") or 0.0)
        except (TypeError, ValueError):
            offset = 0.0
        if anchor == "auto" and layer_id in ("manzanas", "manzana"):
            anchor = "center"
        if anchor == "auto" and layer_id in (
            "localidades_urbana",
            "localidades_rural",
            "ctx_localidades_a",
            "ctx_localidades_urbana",
            "ctx_localidades_rural",
            "ctx_municipios",
            "municipios",
        ):
            anchor = "center"
        if style == "along":
            anchor = "center"
        if layer_id in ("cd", "caserio", "caserio_disperso"):
            anchor = "offset"
        tw, th = _estimate_text_size(text, fs)
        if style == "along" and layer_id in ("ejes", "eje", "vialidades"):
            tw, th = _estimate_along_collision_size(text, fs, angle)
        elif style == "along" and (
            layer_id.startswith("sil_")
            or layer_id in ("corrientes", "ctx_corrientes")
        ):
            tw, th = _estimate_along_collision_size(text, fs, angle)
            tw *= 1.08
            th *= 1.15
        if style == "ageb_oval":
            tw = tw * 1.35 + fs * 1.2
            th = th * 1.55 + fs * 0.4
            if anchor == "auto":
                anchor = "center"
        if layer_id in ("localidades_p", "ctx_localidades_p"):
            # Bloque centrado respecto al punto, pero desplazado (nunca ox=oy=0).
            anchor = "center"
        dist = ((wx - cx) ** 2 + (wy - cy) ** 2) ** 0.5
        span = max(maxx - minx, maxy - miny, 1.0)
        boost = -0.15 if "municipio" in layer_id else 0.0
        if style == "ageb_oval":
            boost -= 0.35
        if layer_id in ("cd", "caserio", "caserio_disperso"):
            boost -= 0.45  # priorizar etiquetas de caserío
        # Vialidades / SIL: antes que manzanas (si no, los números las ahogan)
        if layer_id in ("ejes", "eje", "vialidades"):
            boost -= 0.65
        if layer_id.startswith("sil_"):
            boost -= 0.75
        if layer_id in (
            "localidades_urbana",
            "localidades_rural",
            "ctx_localidades_a",
            "ctx_localidades_urbana",
            "ctx_localidades_rural",
            "ctx_municipios",
            "municipios",
        ):
            boost -= 0.2  # preferir etiqueta en su centroide
        if layer_id in (
            "localidades_urbana",
            "localidades_rural",
            "ctx_localidades_a",
            "ctx_localidades_urbana",
            "ctx_localidades_rural",
        ):
            # Respetar orden de entrada (SQL: ST_Area DESC en localidades_a).
            boost -= max(0.0, 4.0 - float(idx) * 0.008)
        if layer_id in ("manzanas", "manzana"):
            boost += 0.22
        if layer_id in ("localidades_p", "ctx_localidades_p"):
            # Racimos densos después; nombres largos después.
            neigh_r2 = 32.0 * 32.0
            nearby = (
                sum(
                    1
                    for qx, qy in point_page_xy
                    if (px - qx) ** 2 + (py - qy) ** 2 <= neigh_r2
                )
                - 1
            )
            boost += max(0, nearby) * 0.14
            boost += len(text) / 55.0
        if anchor == "center":
            boost -= 0.05
        # Prioridad explícita del fetch (menor = mejor), p.ej. -área.
        try:
            explicit = float(item.get("priority"))
            boost += explicit
        except (TypeError, ValueError):
            pass
        priority = (dist / span) + (len(text) / 80.0) + boost
        # Orden estable: ejes/SIL primero
        tier = 0 if (
            layer_id in ("ejes", "eje", "vialidades")
            or layer_id.startswith("sil_")
            or style == "along"
        ) else 1
        candidates.append(
            _Candidate(
                text=text,
                ax=px,
                ay=py,
                width=tw,
                height=th,
                priority=tier * 10.0 + priority,
                layer_id=layer_id,
                color=str(item.get("color") or "#1a1a1a"),
                bold=bool(item.get("bold", False)),
                italic=bool(item.get("italic", False)),
                font_size=fs,
                style=style,
                anchor=anchor,
                angle=angle,
                offset=offset,
            )
        )

    candidates.sort(key=lambda c: c.priority)

    offsets_center = [
        (0.0, 0.0),
        (3.0, 0.0),
        (-3.0, 0.0),
        (0.0, 3.5),
        (0.0, -3.5),
        (5.0, 4.0),
        (-5.0, 4.0),
        (5.0, -4.0),
        (-5.0, -4.0),
        (8.0, 0.0),
        (-8.0, 0.0),
        (0.0, 8.0),
        (0.0, -8.0),
        (10.0, 8.0),
        (-10.0, 8.0),
    ]
    # Localidades / municipios: casi siempre el centroide (0,0) primero;
    # pocos desplazamientos para no sacar la etiqueta del polígono.
    offsets_loc_center = [
        (0.0, 0.0),
        (4.0, 0.0),
        (-4.0, 0.0),
        (0.0, 4.0),
        (0.0, -4.0),
        (8.0, 6.0),
        (-8.0, 6.0),
    ]
    # CD: etiqueta arriba-derecha del triángulo (ref. PDF INEGI)
    offsets_offset = [
        (6.0, 8.0),
        (8.0, 10.0),
        (4.0, 12.0),
        (10.0, 6.0),
        (-4.0, 10.0),
        (12.0, 12.0),
        (0.0, 14.0),
        (8.0, 2.0),
        (-8.0, 8.0),
        (4.0, -8.0),
    ]

    placed: list[PlacedLabel] = []
    occupied: list[tuple[float, float, float, float]] = list(obstacles or [])

    for cand in candidates:
        if len(placed) >= max_labels:
            break
        lid = str(cand.layer_id or "")
        is_point_loc = lid in ("localidades_p", "ctx_localidades_p")
        is_hydro_along = cand.style == "along" and lid in (
            "corrientes",
            "ctx_corrientes",
        )
        if is_point_loc:
            offsets = _point_label_offsets(cand.font_size)
        elif lid in (
            "localidades_urbana",
            "localidades_rural",
            "ctx_localidades_a",
            "ctx_localidades_urbana",
            "ctx_localidades_rural",
            "ctx_municipios",
            "municipios",
            "cuerpos",
            "ctx_cuerpos",
        ):
            offsets = offsets_loc_center
        else:
            offsets = offsets_center if cand.anchor == "center" else offsets_offset
        # Along: no empujar vialidades arriba/abajo de la calle.
        # SIL sí puede deslizar un poco en perpendicular (página).
        if cand.style == "along":
            import math

            rad = math.radians(float(cand.angle or 0.0))
            ux, uy = math.cos(rad), math.sin(rad)
            nx, ny = -math.sin(rad), math.cos(rad)
            if lid in ("ejes", "eje", "vialidades"):
                # Solo deslizar SOBRE la raya (dirección del ángulo).
                offsets = [(0.0, 0.0)]
                for dist in (5.0, 10.0, 16.0, 24.0, -5.0, -10.0, -16.0, -24.0):
                    offsets.append((ux * dist, uy * dist))
            elif is_hydro_along:
                # Preferir al lado del río (perpendicular), luego deslizar a lo largo.
                offsets = []
                for perp in (5.0, 7.0, 9.0, 11.0, -5.0, -7.0, -9.0, -11.0, 13.0, -13.0):
                    for along in (0.0, 10.0, -10.0, 18.0, -18.0, 28.0, -28.0):
                        offsets.append(
                            (ux * along + nx * perp, uy * along + ny * perp)
                        )
            elif lid.startswith("sil_"):
                offsets = [
                    (0.0, 0.0),
                    (0.0, 4.0),
                    (0.0, -4.0),
                    (3.0, 3.0),
                    (-3.0, 3.0),
                    (5.0, 0.0),
                    (-5.0, 0.0),
                    (0.0, 7.0),
                    (0.0, -7.0),
                ]
            else:
                offsets = [(0.0, 0.0), (3.0, 0.0), (-3.0, 0.0), (0.0, 3.0), (0.0, -3.0)]
        # AGEB: preferir desplazamientos amplios lejos del centro denso
        if cand.style == "ageb_oval":
            offsets = [
                (0.0, 0.0),
                (18.0, 12.0),
                (-18.0, 12.0),
                (22.0, -8.0),
                (-22.0, -8.0),
                (28.0, 0.0),
                (-28.0, 0.0),
                (0.0, 24.0),
                (0.0, -24.0),
                (14.0, 20.0),
                (-14.0, 20.0),
            ] + list(offsets_center)

        pad_use = (
            0.15
            if cand.style == "along"
            and lid.startswith(("ejes", "eje", "vial"))
            else (
                1.15
                if is_point_loc
                else (0.55 if is_hydro_along else (0.45 if cand.style == "along" else padding))
            )
        )
        extra_obs: list[tuple[float, float, float, float]] = []
        if is_point_loc:
            # Halo alrededor del punto: la etiqueta no debe tapar el símbolo.
            hr = max(3.8, float(cand.font_size) * 0.55)
            extra_obs.append((cand.ax - hr, cand.ay - hr, hr * 2.0, hr * 2.0))

        use_smart = is_point_loc or is_hydro_along
        if use_smart:
            # Puntos: solo hueco libre (score 0). Si no cabe → se omite.
            max_sc = 0 if is_point_loc else 10**9
            box = _pick_best_label_box(
                cand=cand,
                offsets=offsets,
                frame=frame,
                occupied=occupied,
                padding=pad_use,
                extra_obstacles=extra_obs,
                margin=1.5,
                max_score=max_sc,
            )
            if box is not None:
                placed.append(
                    PlacedLabel(
                        text=cand.text,
                        x=box[0],
                        y=box[1],
                        width=cand.width,
                        height=cand.height,
                        layer_id=cand.layer_id,
                        color=cand.color,
                        bold=cand.bold,
                        italic=bool(getattr(cand, "italic", False)),
                        font_size=cand.font_size,
                        style=cand.style,
                        angle=float(getattr(cand, "angle", 0.0) or 0.0),
                        offset=float(getattr(cand, "offset", 0.0) or 0.0),
                    )
                )
                occupied.append(box)
                continue
            # Sin hueco bueno: no forzar encima del río/punto
            if is_point_loc:
                continue
            # Corriente: último intento con el mejor score aunque solape un poco
            box = _pick_best_label_box(
                cand=cand,
                offsets=offsets,
                frame=frame,
                occupied=occupied,
                padding=0.1,
                extra_obstacles=(),
                margin=1.0,
                max_score=10**9,
            )
            if box is None:
                continue
            placed.append(
                PlacedLabel(
                    text=cand.text,
                    x=box[0],
                    y=box[1],
                    width=cand.width,
                    height=cand.height,
                    layer_id=cand.layer_id,
                    color=cand.color,
                    bold=cand.bold,
                    italic=bool(getattr(cand, "italic", False)),
                    font_size=cand.font_size,
                    style=cand.style,
                    angle=float(getattr(cand, "angle", 0.0) or 0.0),
                    offset=float(getattr(cand, "offset", 0.0) or 0.0),
                )
            )
            occupied.append(box)
            continue

        for ox, oy in offsets:
            box = _box_for_anchor(
                cand.ax, cand.ay, cand.width, cand.height, ox, oy, cand.anchor
            )
            if not _inside_frame(
                box,
                frame,
                margin=0.5 if cand.layer_id in ("cd", "caserio", "caserio_disperso") else 2.0,
            ):
                continue
            if any(
                _boxes_overlap(
                    box,
                    other,
                    0.15 if cand.style == "along" and str(cand.layer_id or "").startswith(("ejes", "eje", "vial"))
                    else (0.45 if cand.style == "along" else padding),
                )
                for other in occupied
            ):
                continue
            placed.append(
                PlacedLabel(
                    text=cand.text,
                    x=box[0],
                    y=box[1],
                    width=cand.width,
                    height=cand.height,
                    layer_id=cand.layer_id,
                    color=cand.color,
                    bold=cand.bold,
                    italic=bool(getattr(cand, "italic", False)),
                    font_size=cand.font_size,
                    style=cand.style,
                    angle=float(getattr(cand, "angle", 0.0) or 0.0),
                    offset=float(getattr(cand, "offset", 0.0) or 0.0),
                )
            )
            # Manzanas: permitir solape (se dibujan todas). AGEB sí reserva hueco.
            if cand.layer_id not in ("manzanas", "manzana"):
                occupied.append(box)
            break
        else:
            # Sin hueco libre: forzar manzanas/AGEB o vialidad en la raya
            force_dense = cand.layer_id in ("manzanas", "manzana") or cand.style == "ageb_oval"
            force_along = cand.style == "along" and str(cand.layer_id or "") in (
                "ejes",
                "eje",
                "vialidades",
            )
            if not (force_dense or force_along):
                continue
            box = _box_for_anchor(
                cand.ax,
                cand.ay,
                cand.width,
                cand.height,
                0.0,
                0.0,
                "center" if force_dense else cand.anchor,
            )
            margin = 0.2 if force_dense else 1.0
            if not _inside_frame(box, frame, margin=margin):
                continue
            placed.append(
                PlacedLabel(
                    text=cand.text,
                    x=box[0],
                    y=box[1],
                    width=cand.width,
                    height=cand.height,
                    layer_id=cand.layer_id,
                    color=cand.color,
                    bold=cand.bold,
                    italic=bool(getattr(cand, "italic", False)),
                    font_size=cand.font_size,
                    style=cand.style,
                    angle=float(getattr(cand, "angle", 0.0) or 0.0),
                    offset=0.0 if force_along else float(getattr(cand, "offset", 0.0) or 0.0),
                )
            )
            if cand.style == "ageb_oval" or force_along:
                occupied.append(box)

    return placed
