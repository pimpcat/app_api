"""Ensamblado PDF con ReportLab Canvas (salida 100% vectorial)."""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from typing import Any, Optional, Sequence

from reportlab.lib.colors import HexColor
from reportlab.pdfgen.canvas import Canvas
from shapely.geometry.base import BaseGeometry

from cartography_engine.branding import get_branding
from cartography_engine.identity import draw_brand_footer, draw_brand_header
from cartography_engine.label_collision import resolve_label_collisions
from cartography_engine.layouts import Box, PageLayout, build_layout
from cartography_engine.layers import LayerData, LegendItem
from cartography_engine.renderers import (
    compute_map_scale,
    draw_demo_polygon,
    draw_frame,
    draw_geometry,
    draw_legend,
    draw_north_arrow,
    draw_scale_bar,
    draw_title,
)
from cartography_engine.symbols import PolygonSymbol, polygon_symbol_from_dict


def _is_area_localidad_layer(layer_id: str) -> bool:
    """Localidades de área (foco + ctx): relleno amarillo, no solo borde."""
    return str(layer_id or "") in (
        "localidades_urbana",
        "localidades_rural",
        "ctx_localidades_a",
        "ctx_localidades_urbana",
        "ctx_localidades_rural",
    )


def _is_hydro_overlay_layer(layer_id: str) -> bool:
    """SIL/SIA se dibujan en pases dedicados (no en el loop base).

    ``corrientes`` / ``cuerpos`` del croquis van en orden de plantilla
    (detrás de localidades de área); no se posponen aquí.
    """
    lid = str(layer_id or "")
    return (
        lid in ("sil", "sia")
        or lid.startswith("sil_")
        or lid.startswith("sia_")
    )


def _is_point_overlay_layer(layer: Any) -> bool:
    """Puntos encima de rellenos/líneas (pase final)."""
    defn = getattr(layer, "definition", None)
    if defn is None:
        return False
    lid = str(getattr(defn, "id", "") or "")
    return lid in (
        "localidades_p",
        "ctx_localidades_p",
        "aeropuertos",
        "ctx_aeropuertos",
        "aeropuerto_intl",
        "aeropuerto_local",
    ) or lid.startswith("aeropuerto")


def _is_municipio_limite_overlay(layer_id: str) -> bool:
    """Límite municipal encima de AGEB (foco y vecinos; casamiento + dash)."""
    return str(layer_id or "") in (
        "municipio_limite",
        "municipios_l",
        "ctx_municipio_limite",
    )


def _is_estado_limite_overlay(layer_id: str) -> bool:
    """Límite estatal (+++) encima del municipal (no taparlo)."""
    return str(layer_id or "") in ("estados_l", "estado_limite", "ctx_estados_l")


def _along_label_offset_mult(layer_id: str, explicit: Any = None) -> float:
    """
    Offset perpendicular (× font-size) según tipo de capa.
    ejes → 0 (centrado en la calle); SIL → aire; carretera → más aire.
    """
    try:
        if explicit is not None and str(explicit).strip() != "":
            val = float(explicit)
            if val >= 0:
                return val
    except (TypeError, ValueError):
        pass
    lid = str(layer_id or "")
    if lid in ("ejes", "eje", "vialidades", "corrientes", "ctx_corrientes"):
        return 0.0
    if lid == "sil_carretera":
        return 1.85
    if lid.startswith("sil_"):
        return 1.35
    return 0.0


def _begin_map_clip(c: Canvas, frame: Box) -> None:
    """Recorta dibujo al marco del mapa (evita elementos sobre el borde)."""
    c.saveState()
    p = c.beginPath()
    p.rect(frame.x, frame.y, frame.width, frame.height)
    c.clipPath(p, stroke=0, fill=0)


def _end_map_clip(c: Canvas) -> None:
    c.restoreState()


def draw_map_page(
    c: Canvas,
    *,
    layout: PageLayout,
    title: str,
    footer: str,
    geometry: Optional[BaseGeometry] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    symbol: Optional[PolygonSymbol] = None,
    layers: Optional[Sequence[LayerData]] = None,
    legend_items: Optional[Sequence[LegendItem]] = None,
    map_scale: Optional[float] = None,
    demo: bool = False,
    brand_subtitle: Optional[str] = None,
    labels: Optional[Sequence[dict]] = None,
    max_labels: int = 60,
    strip_content: Any = None,
    croquis_panel_content: Any = None,
    condensado_panel_content: Any = None,
    sip_features: Optional[Sequence[dict]] = None,
    cd_features: Optional[Sequence[dict]] = None,
    assembly_tiles: Optional[Sequence[tuple[float, float, float, float]]] = None,
    label_page_cap: Optional[float] = None,
) -> None:
    """Dibuja una hoja de mapa en el canvas actual (sin showPage/save)."""
    if layout.show_outer_frame:
        draw_frame(c, layout.outer_frame, stroke=1.8)
    if layout.show_brand_header and layout.brand_header.height > 0:
        draw_brand_header(c, layout.brand_header, subtitle=brand_subtitle)
    draw_frame(c, layout.map_frame, stroke=0.9)
    if layout.title.height > 0 and title:
        draw_title(c, layout.title, title)
    if layout.north_enabled and layout.north.height > 0:
        draw_north_arrow(c, layout.north)

    scale_value = map_scale
    map_content = not demo and (layers or (geometry is not None and bounds is not None))
    if map_content and bounds is not None:
        _begin_map_clip(c, layout.map_frame)
    if demo:
        draw_demo_polygon(c, layout.map_frame)
        scale_value = scale_value or 50000.0
    elif layers:
        if bounds is None:
            raise ValueError("bounds requerido para capas")
        for layer in layers:
            if layer.geometry is None or layer.geometry.is_empty:
                continue
            if not getattr(layer.definition, "draw", True):
                continue
            # SIP clasificado se dibuja aparte
            if sip_features and layer.definition.id == "sip":
                continue
            # CD se dibuja siempre con drawer dedicado (triángulo + etiqueta)
            if layer.definition.id == "cd":
                continue
            # Localidad: se redibuja al final (dash rojo encima de todo)
            if layer.definition.id == "localidad":
                continue
            # AGEB urbano/rural + colindantes: encima de manzanas (pase dedicado)
            if layer.definition.id in ("ageb", "ageb_rural", "colindantes"):
                continue
            # Localidades de área: pase dedicado DESPUÉS de AGEB (relleno visible)
            if _is_area_localidad_layer(layer.definition.id):
                continue
            # Hidro (SIL/SIA) al final: encima de manzanas/vialidad
            if _is_hydro_overlay_layer(layer.definition.id):
                continue
            # Puntos encima de líneas (pase final)
            if _is_point_overlay_layer(layer):
                continue
            # Límite municipal: encima de AGEB (pase dedicado)
            if _is_municipio_limite_overlay(layer.definition.id):
                continue
            # Límite estatal: encima del municipal (pase dedicado)
            if _is_estado_limite_overlay(layer.definition.id):
                continue
            draw_geometry(
                c,
                layer.geometry,
                bounds,
                layout.map_frame,
                layer.definition.symbol,
            )
        if scale_value is None:
            scale_value = compute_map_scale(bounds, layout.map_frame)
    elif geometry is not None and bounds is not None:
        draw_geometry(c, geometry, bounds, layout.map_frame, symbol)
        if scale_value is None:
            scale_value = compute_map_scale(bounds, layout.map_frame)

    if scale_value is None and bounds is not None:
        scale_value = compute_map_scale(bounds, layout.map_frame)

    if sip_features and bounds:
        from cartography_engine.symbols.sip_icons import (
            draw_sip_features_on_map,
            sip_page_obstacles,
        )

        draw_sip_features_on_map(
            c,
            sip_features,
            bounds,
            layout.map_frame,
            map_scale=scale_value,
        )
        obstacles = sip_page_obstacles(
            sip_features,
            bounds,
            layout.map_frame,
            map_scale=scale_value,
        )
    else:
        obstacles = []

    # CD: drawer dedicado — fetch etiquetado; si vacío, geometría de capa (ya filtrada)
    cd_draw = list(cd_features or [])
    if not cd_draw and layers:
        for layer in layers:
            if getattr(getattr(layer, "definition", None), "id", "") != "cd":
                continue
            geom = getattr(layer, "geometry", None)
            if geom is not None and not getattr(geom, "is_empty", True):
                from cartography_engine.datasource import cd_features_from_geometry

                cd_draw = cd_features_from_geometry(geom)
            break
    if cd_draw and bounds:
        from cartography_engine.renderers import draw_cd_features_on_map

        draw_cd_features_on_map(
            c,
            cd_draw,
            bounds,
            layout.map_frame,
            map_scale=scale_value,
        )

    # SIA / carretera / canal (antes del límite)
    if layers and bounds:
        by_id = {
            getattr(getattr(L, "definition", None), "id", ""): L
            for L in layers
            if L.geometry is not None and not L.geometry.is_empty
        }
        early_hydro = [
            "sia",
            "sia_industrial",
            "sia_diversa",
            "sia_cementerio",
            "sia_deportiva",
            "sia_agua",
            "sia_otros",
            "sil_carretera",
        ]
        drawn_early = set()
        for hid in early_hydro:
            layer = by_id.get(hid)
            if layer is None:
                continue
            # sia_* generados por prefijo
            draw_geometry(
                c,
                layer.geometry,
                bounds,
                layout.map_frame,
                layer.definition.symbol,
            )
            drawn_early.add(hid)
        for layer in layers:
            lid = str(getattr(getattr(layer, "definition", None), "id", "") or "")
            if lid in drawn_early:
                continue
            if lid.startswith("sia") and lid != "sia":
                draw_geometry(
                    c,
                    layer.geometry,
                    bounds,
                    layout.map_frame,
                    layer.definition.symbol,
                )
                drawn_early.add(lid)

    # AGEB rural (naranja dash) + colindantes + AGEB urbano encima de manzanas
    if layers and bounds:
        for want_id in ("ageb_rural", "colindantes", "ageb"):
            for layer in layers:
                if getattr(getattr(layer, "definition", None), "id", "") != want_id:
                    continue
                if layer.geometry is None or layer.geometry.is_empty:
                    continue
                draw_geometry(
                    c,
                    layer.geometry,
                    bounds,
                    layout.map_frame,
                    layer.definition.symbol,
                )

    # Localidades de área (foco + ctx) ENCIMA del AGEB: relleno urbana/rural visible
    if layers and bounds:
        for layer in layers:
            lid = str(getattr(getattr(layer, "definition", None), "id", "") or "")
            if not _is_area_localidad_layer(lid):
                continue
            if layer.geometry is None or layer.geometry.is_empty:
                continue
            if not getattr(layer.definition, "draw", True):
                continue
            draw_geometry(
                c,
                layer.geometry,
                bounds,
                layout.map_frame,
                layer.definition.symbol,
            )

    # Límite municipal ENCIMA de AGEB: casamiento blanco + dash verde
    # (foco y vecinos; el dash necesita linemerge vía _draw_lines).
    if layers and bounds:
        from cartography_engine.renderers import _draw_lines

        lim_layers = []
        for layer in layers:
            if _is_municipio_limite_overlay(
                getattr(getattr(layer, "definition", None), "id", "")
            ):
                if layer.geometry is not None and not layer.geometry.is_empty:
                    lim_layers.append(layer)
        if not lim_layers:
            # Fallback: perímetro del polígono municipal
            for layer in layers:
                if getattr(getattr(layer, "definition", None), "id", "") != "municipio":
                    continue
                g = getattr(layer, "geometry", None)
                if g is None or getattr(g, "is_empty", True):
                    continue
                try:
                    from dataclasses import replace

                    from cartography_engine.layers import LayerData
                    from cartography_engine.symbols import LineSymbol

                    lim_layers.append(
                        LayerData(
                            definition=replace(
                                layer.definition,
                                id="municipio_limite",
                                symbol=LineSymbol(
                                    stroke_color="#24C200",
                                    stroke_width=2.4,
                                    dash=(10.0, 5.0),
                                ),
                            ),
                            geometry=g.boundary,
                            feature_count=1,
                        )
                    )
                except Exception:
                    pass
                break
        for lim_layer in lim_layers:
            sym = lim_layer.definition.symbol
            stroke = str(getattr(sym, "stroke_color", "#24C200") or "#24C200")
            width = float(getattr(sym, "stroke_width", 3.0) or 3.0)
            if str(getattr(lim_layer.definition, "id", "") or "").startswith("ctx_"):
                width = max(width, 2.8)
            dash = getattr(sym, "dash", None) or (14.0, 10.0)
            # Casamiento blanco sólido debajo; verde con dash (gaps visibles)
            _draw_lines(
                c,
                lim_layer.geometry,
                bounds,
                layout.map_frame,
                "#FFFFFF",
                max(width * 2.15, width + 2.2),
                None,
                None,
            )
            _draw_lines(
                c,
                lim_layer.geometry,
                bounds,
                layout.map_frame,
                stroke,
                width,
                tuple(float(x) for x in dash),
                None,
            )

    # Límite estatal (+++) ENCIMA del municipal: no lo tapa el dash verde
    if layers and bounds:
        for layer in layers:
            if not _is_estado_limite_overlay(
                getattr(getattr(layer, "definition", None), "id", "")
            ):
                continue
            if layer.geometry is None or layer.geometry.is_empty:
                continue
            if not getattr(layer.definition, "draw", True):
                continue
            draw_geometry(
                c,
                layer.geometry,
                bounds,
                layout.map_frame,
                layer.definition.symbol,
            )

    # Límite de localidad (sólido rojo; encima de AGEB)
    if layers and bounds:
        for layer in layers:
            if getattr(getattr(layer, "definition", None), "id", "") != "localidad":
                continue
            if layer.geometry is None or layer.geometry.is_empty:
                continue
            draw_geometry(
                c,
                layer.geometry,
                bounds,
                layout.map_frame,
                layer.definition.symbol,
            )
            break

    # Canal / SIL corriente ENCIMA; corrientes/cuerpos del croquis ya van en el loop
    if layers and bounds:
        by_id = {
            getattr(getattr(L, "definition", None), "id", ""): L
            for L in layers
            if L.geometry is not None and not L.geometry.is_empty
        }
        for hid in ("sil_canal", "sil_corriente", "sil"):
            layer = by_id.get(hid)
            if layer is None:
                continue
            draw_geometry(
                c,
                layer.geometry,
                bounds,
                layout.map_frame,
                layer.definition.symbol,
            )

    # Puntos (localidades / aeropuertos) encima de líneas e hidro
    if layers and bounds:
        for layer in layers:
            if not _is_point_overlay_layer(layer):
                continue
            if layer.geometry is None or layer.geometry.is_empty:
                continue
            draw_geometry(
                c,
                layer.geometry,
                bounds,
                layout.map_frame,
                layer.definition.symbol,
            )

    # Quitar etiquetas CD del motor de colisión (ya van con el triángulo)
    if labels:
        labels = [lab for lab in labels if str(lab.get("layer_id") or "") != "cd"]

    if labels and bounds:
        # A escalas urbanas (p.ej. Chilpancingo ~1:50k) bajar tipografía global.
        dens = 1.0
        sv = float(scale_value or 0.0)
        if sv >= 50000:
            dens = 0.55
        elif sv >= 35000:
            dens = 0.65
        elif sv >= 20000:
            dens = 0.78
        label_fs = max(4.5, min(12.0, layout.page_width * 0.0042 * dens))
        if label_page_cap is not None:
            try:
                label_fs = min(label_fs, float(label_page_cap))
            except (TypeError, ValueError):
                pass
        # Orden: SIL (hidro) → vialidades → corrientes/cuerpos → resto.
        # Así las corrientes no “pierden” etiqueta por choque con NINGUNO.
        sil_labs = [
            lab
            for lab in labels
            if str(lab.get("layer_id") or "").startswith("sil_")
        ]
        ejes_labs = [
            lab
            for lab in labels
            if str(lab.get("layer_id") or "") == "ejes"
        ]
        hydro_labs = [
            lab
            for lab in labels
            if str(lab.get("layer_id") or "")
            in ("corrientes", "cuerpos", "ctx_corrientes", "ctx_cuerpos")
        ]
        loc_area_labs = [
            lab
            for lab in labels
            if str(lab.get("layer_id") or "")
            in (
                "localidades_urbana",
                "localidades_rural",
                "ctx_localidades_urbana",
                "ctx_localidades_rural",
            )
        ]
        rest_labs = [
            lab
            for lab in labels
            if str(lab.get("layer_id") or "")
            not in (
                "ejes",
                "corrientes",
                "cuerpos",
                "ctx_corrientes",
                "ctx_cuerpos",
                "localidades_urbana",
                "localidades_rural",
                "ctx_localidades_urbana",
                "ctx_localidades_rural",
            )
            and not str(lab.get("layer_id") or "").startswith("sil_")
        ]
        mza_labs = [
            lab for lab in rest_labs if str(lab.get("layer_id") or "") in ("manzanas", "manzana")
        ]
        ageb_labs = [
            lab
            for lab in rest_labs
            if str(lab.get("layer_id") or "") == "ageb"
            or str(lab.get("style") or "") == "ageb_oval"
        ]
        other_rest = [
            lab
            for lab in rest_labs
            if lab not in mza_labs and lab not in ageb_labs
        ]
        # Si la etiqueta trae size de plantilla, amortiguarlo a escala urbana
        # (manzanas/AGEB se amortiguan menos para seguir legibles).
        if dens < 0.99:
            for lab in labels:
                lid = str(lab.get("layer_id") or "")
                style = str(lab.get("style") or "")
                try:
                    sz = float(lab.get("size") or 0)
                except (TypeError, ValueError):
                    sz = 0.0
                if sz <= 0:
                    continue
                if lid in ("manzanas", "manzana"):
                    lab["size"] = max(2.2, sz * max(dens, 0.72))
                elif lid in ("ctx_municipios", "municipios", "entidad"):
                    # Etiquetas de municipio / entidad: no amortiguar
                    lab["size"] = sz
                elif lid in (
                    "localidades_urbana",
                    "localidades_rural",
                    "ctx_localidades_urbana",
                    "ctx_localidades_rural",
                ):
                    lab["size"] = max(5.8, sz * max(dens, 0.88))
                elif lid in ("localidades_p", "ctx_localidades_p"):
                    lab["size"] = max(4.4, sz * max(dens, 0.82))
                elif lid in ("corrientes", "ctx_corrientes"):
                    lab["size"] = max(5.5, sz * max(dens, 0.9))
                elif lid in ("cuerpos", "ctx_cuerpos"):
                    lab["size"] = max(5.0, sz * max(dens, 0.9))
                elif lid == "colindantes":
                    # Rurales colindantes: más grandes y poco amortiguadas
                    lab["size"] = max(4.8, sz * max(dens, 0.92))
                elif style == "ageb_oval" or lid in ("ageb", "ageb_rural"):
                    if lid == "ageb_rural":
                        lab["size"] = max(10.5, sz * max(dens, 0.90))
                    elif lid == "ageb":
                        lab["size"] = max(2.2, sz * max(dens, 0.78))
                    else:
                        lab["size"] = max(3.0, sz * max(dens, 0.8))
                elif lid == "ejes":
                    # Overview urbano: respetar tipografía chica de plantilla (~2.5)
                    lab["size"] = max(2.2, sz * dens)
                else:
                    lab["size"] = max(2.6, sz * dens)
        placed = []
        if sil_labs:
            placed.extend(
                resolve_label_collisions(
                    sil_labs,
                    bounds=bounds,
                    frame=layout.map_frame,
                    font_size=max(3.6, min(7.0, label_fs * 0.85)),
                    padding=0.5,
                    max_labels=min(80, max(1, int(max_labels))),
                    obstacles=list(obstacles or []),
                )
            )
        if ejes_labs:
            # Cartas de detalle (~1:7.5k): dibujar TODAS las vialidades sin colisión
            # (el colisionador descartaba la mayoría en trama densa).
            # Overview urbano (~1:20k+) conserva colisión + tope.
            if sv > 0 and sv <= 12000:
                from cartography_engine.renderers import world_to_page as _w2p
                from cartography_engine.label_collision import PlacedLabel

                for lab in ejes_labs:
                    geom = lab.get("geometry")
                    if geom is None:
                        continue
                    txt = str(lab.get("text") or "").strip()
                    if not txt:
                        continue
                    try:
                        wx, wy = float(geom.x), float(geom.y)
                    except Exception:
                        try:
                            rp = geom.representative_point()
                            wx, wy = float(rp.x), float(rp.y)
                        except Exception:
                            continue
                    if not (
                        bounds[0] - 1 <= wx <= bounds[2] + 1
                        and bounds[1] - 1 <= wy <= bounds[3] + 1
                    ):
                        continue
                    px, py = _w2p(wx, wy, bounds, layout.map_frame)
                    fs = float(lab.get("size") or 3.1)
                    fs = max(2.6, min(4.5, fs))
                    tw = max(6.0, len(txt) * fs * 0.50)
                    th = fs * 1.15
                    placed.append(
                        PlacedLabel(
                            text=txt[:48],
                            x=px - tw / 2.0,
                            y=py - th / 2.0,
                            width=tw,
                            height=th,
                            layer_id="ejes",
                            color=str(lab.get("color") or "#1a1a1a"),
                            bold=False,
                            font_size=fs,
                            style="along",
                            angle=float(lab.get("angle") or 0.0),
                            offset=0.0,
                        )
                    )
            else:
                placed.extend(
                    resolve_label_collisions(
                        ejes_labs,
                        bounds=bounds,
                        frame=layout.map_frame,
                        font_size=max(3.2, min(5.5, label_fs * 0.72)),
                        padding=0.2,
                        max_labels=min(380, max(1, int(max_labels) + 40)),
                        obstacles=list(obstacles or []),
                    )
                )
            # Si el colisionador no colocó ninguna (p.ej. coords fuera), dibujar crudo
            if not any(getattr(p, "layer_id", "") == "ejes" for p in placed):
                from cartography_engine.renderers import world_to_page as _w2p
                from cartography_engine.label_collision import PlacedLabel

                for lab in ejes_labs[:80]:
                    geom = lab.get("geometry")
                    if geom is None:
                        continue
                    try:
                        wx, wy = float(geom.x), float(geom.y)
                    except Exception:
                        try:
                            rp = geom.representative_point()
                            wx, wy = float(rp.x), float(rp.y)
                        except Exception:
                            continue
                    if not (bounds[0] <= wx <= bounds[2] and bounds[1] <= wy <= bounds[3]):
                        continue
                    px, py = _w2p(wx, wy, bounds, layout.map_frame)
                    txt = str(lab.get("text") or "")
                    fs = float(lab.get("size") or 2.5)
                    placed.append(
                        PlacedLabel(
                            text=txt,
                            x=px - len(txt) * fs * 0.25,
                            y=py,
                            width=len(txt) * fs * 0.5,
                            height=fs * 1.2,
                            layer_id="ejes",
                            color=str(lab.get("color") or "#1a1a1a"),
                            bold=True,
                            font_size=fs,
                            style="along",
                            angle=float(lab.get("angle") or 0.0),
                            offset=0.0,
                        )
                    )
        if hydro_labs:
            placed.extend(
                resolve_label_collisions(
                    hydro_labs,
                    bounds=bounds,
                    frame=layout.map_frame,
                    font_size=max(4.5, min(8.0, label_fs * 0.95)),
                    padding=0.35,
                    max_labels=min(400, max(1, int(max_labels))),
                    obstacles=list(obstacles or [])
                    + [(p.x, p.y, p.width, p.height) for p in placed],
                )
            )
        if loc_area_labs:
            placed.extend(
                resolve_label_collisions(
                    loc_area_labs,
                    bounds=bounds,
                    frame=layout.map_frame,
                    font_size=max(5.5, min(9.0, label_fs * 1.05)),
                    padding=0.4,
                    max_labels=min(700, max(1, int(max_labels))),
                    obstacles=list(obstacles or [])
                    + [(p.x, p.y, p.width, p.height) for p in placed],
                )
            )
        along_boxes = [(p.x, p.y, p.width, p.height) for p in placed]
        # Colocar AGEB tras manzanas (evitan solape si pueden), pero se DIBUJAN al final.
        if mza_labs:
            placed.extend(
                resolve_label_collisions(
                    mza_labs,
                    bounds=bounds,
                    frame=layout.map_frame,
                    font_size=max(2.2, min(5.5, label_fs * 0.55)),
                    padding=0.05,
                    max_labels=max(1, min(len(mza_labs) + 50, 50000)),
                    obstacles=list(obstacles or []) + along_boxes,
                )
            )
        mza_boxes = [
            (p.x, p.y, p.width, p.height)
            for p in placed
            if str(getattr(p, "layer_id", "")) in ("manzanas", "manzana")
        ]
        placed.extend(
            resolve_label_collisions(
                other_rest,
                bounds=bounds,
                frame=layout.map_frame,
                font_size=label_fs,
                padding=2.4,
                max_labels=max(1, min(int(max_labels), 2000)),
                obstacles=list(obstacles or []) + along_boxes + mza_boxes,
            )
        )
        if ageb_labs:
            placed.extend(
                resolve_label_collisions(
                    ageb_labs,
                    bounds=bounds,
                    frame=layout.map_frame,
                    font_size=max(3.5, min(8.0, label_fs * 0.9)),
                    padding=1.2,
                    max_labels=max(1, min(len(ageb_labs) + 20, 2000)),
                    obstacles=list(obstacles or []) + along_boxes + mza_boxes,
                )
            )

        def _draw_one_label(item) -> None:
            color = getattr(item, "color", None) or "#1a1a1a"
            try:
                fill = HexColor(str(color))
            except Exception:
                fill = HexColor("#1a1a1a")
            fs = float(getattr(item, "font_size", 0) or label_fs)
            bold = bool(getattr(item, "bold", False))
            italic = bool(getattr(item, "italic", False))
            if bold and italic:
                font = "Helvetica-BoldOblique"
            elif italic:
                font = "Helvetica-Oblique"
            elif bold:
                font = "Helvetica-Bold"
            else:
                font = "Helvetica"
            style = str(getattr(item, "style", "") or "")
            angle = float(getattr(item, "angle", 0.0) or 0.0)
            lid = str(getattr(item, "layer_id", "") or "")
            if style == "ageb_oval":
                # Urbana: óvalo/texto más fino; colindantes: un poco más marcado
                urban = lid == "ageb"
                pad_x = fs * (0.28 if urban else 0.38)
                pad_y = fs * (0.18 if urban else 0.24)
                ex0 = item.x - pad_x * 0.1
                ey0 = item.y - pad_y
                ex1 = item.x + item.width + pad_x * 0.1
                ey1 = item.y + item.height * 0.72 + pad_y
                c.setStrokeColor(fill)
                c.setLineWidth(
                    max(0.22, fs * 0.055) if urban else max(0.4, fs * 0.09)
                )
                c.setDash([], 0)
                c.ellipse(ex0, ey0, ex1, ey1, stroke=1, fill=0)
            c.setFillColor(fill)
            c.setFont(font, fs)
            lines = str(item.text or "").split("\n")
            leading = fs * 1.15

            def _draw_lines_at(x0: float, y_bottom: float, centered: bool = False) -> None:
                n = max(1, len(lines))
                for i, line in enumerate(lines):
                    ly = y_bottom + (n - 1 - i) * leading
                    if centered:
                        tw = c.stringWidth(line, font, fs)
                        c.drawString(x0 + (item.width - tw) / 2.0, ly, line)
                    else:
                        c.drawString(x0, ly, line)

            if style == "along":
                import math

                cx = item.x + item.width / 2.0
                cy = item.y + item.height / 2.0
                off_mult = _along_label_offset_mult(
                    lid, getattr(item, "offset", None)
                )
                ox = oy = 0.0
                if off_mult > 0.05:
                    rad = math.radians(angle + 90.0)
                    ox = math.cos(rad) * fs * off_mult
                    oy = math.sin(rad) * fs * off_mult
                c.saveState()
                c.translate(cx + ox, cy + oy)
                if abs(angle) > 0.3:
                    c.rotate(angle)
                # Along: una sola línea habitual; si hay \n, centrar bloque.
                n = max(1, len(lines))
                for i, line in enumerate(lines):
                    ly = -fs * 0.28 + (n - 1 - i) * leading - (n - 1) * leading / 2.0
                    c.drawCentredString(0, ly, line)
                c.restoreState()
            elif style == "ageb_oval" or lid in (
                "manzanas",
                "manzana",
                "ctx_municipios",
                "municipios",
                "localidades_urbana",
                "localidades_rural",
                "localidades_p",
                "ctx_localidades_a",
                "ctx_localidades_urbana",
                "ctx_localidades_rural",
                "ctx_localidades_p",
                "cuerpos",
                "ctx_cuerpos",
            ):
                # Bloque multilínea centrado (clave encima, nombre debajo alineados al centro)
                n = max(1, len(lines))
                block_h = (n - 1) * leading + fs
                ty = item.y + max(0.0, (item.height - block_h) / 2.0)
                _draw_lines_at(item.x, ty, centered=True)
            else:
                _draw_lines_at(item.x, item.y, centered=False)

        def _is_ageb_label(item) -> bool:
            return str(getattr(item, "style", "") or "") == "ageb_oval" or str(
                getattr(item, "layer_id", "") or ""
            ) in ("ageb", "colindantes")

        # 1) todo lo demás  2) AGEB al final (encima de todo el mapa)
        for item in placed:
            if not _is_ageb_label(item):
                _draw_one_label(item)
        for item in placed:
            if _is_ageb_label(item):
                _draw_one_label(item)

    # Grilla de armado (hoja índice plotter): encima del mapa, dentro del clip
    if assembly_tiles and bounds is not None:
        from cartography_engine.pdf.strip import draw_assembly_grid_on_map

        draw_assembly_grid_on_map(
            c,
            assembly_tiles,
            bounds,
            layout.map_frame,
        )

    if map_content and bounds is not None:
        _end_map_clip(c)

    if condensado_panel_content is not None and layout.legend:
        from cartography_engine.pdf.condensado_panel import (
            CondensadoPanelContent,
            draw_condensado_panel,
        )

        panel = condensado_panel_content
        if isinstance(panel, dict):
            panel = CondensadoPanelContent(**panel)
        if isinstance(panel, CondensadoPanelContent):
            if (not panel.escala) and scale_value:
                panel = replace(panel, escala=float(scale_value))
            draw_condensado_panel(c, layout.legend, panel, legend_items=legend_items)
    elif croquis_panel_content is not None and layout.legend:
        from cartography_engine.pdf.croquis_panel import (
            CroquisPanelContent,
            draw_croquis_panel,
        )

        panel = croquis_panel_content
        if isinstance(panel, dict):
            panel = CroquisPanelContent(**panel)
        if isinstance(panel, CroquisPanelContent):
            if (not panel.escala) and scale_value:
                panel = replace(panel, escala=float(scale_value))
            draw_croquis_panel(c, layout.legend, panel, legend_items=legend_items)
    elif legend_items and layout.legend:
        draw_legend(c, layout.legend, legend_items)

    if layout.scale_bar_enabled and layout.scale_bar.height > 0:
        draw_scale_bar(c, layout.scale_bar, float(scale_value or 0.0))
    if layout.footer.height > 0 and footer:
        draw_brand_footer(c, layout.footer, footer)

    if getattr(layout, "strip_enabled", False) and layout.strip is not None and strip_content is not None:
        from cartography_engine.pdf.strip import StripContent, draw_info_strip

        sc = strip_content
        if isinstance(sc, dict):
            sc = StripContent(**sc)
        if isinstance(sc, StripContent) and (not sc.escala) and scale_value:
            sc = replace(sc, escala=float(scale_value))
        draw_info_strip(c, layout.strip, sc)


def draw_cover_page(
    c: Canvas,
    *,
    layout: PageLayout,
    title: str,
    subtitle: str,
    municipalities: Sequence[dict[str, str]],
    footer: str,
) -> None:
    """Portada de atlas: marca + título + listado de municipios."""
    if layout.show_outer_frame:
        draw_frame(c, layout.outer_frame, stroke=1.8)
    if layout.show_brand_header and layout.brand_header.height > 0:
        draw_brand_header(c, layout.brand_header, subtitle=subtitle)
    draw_title(c, layout.title, title)

    body = layout.map_frame
    c.setFillColor(HexColor("#1F4E79"))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(body.x + 8, body.y2 - 18, f"Municipios incluidos ({len(municipalities)})")

    c.setStrokeColor(HexColor("#CCCCCC"))
    c.setLineWidth(0.5)
    c.line(body.x + 8, body.y2 - 24, body.x2 - 8, body.y2 - 24)

    y = body.y2 - 40
    col_gap = body.width / 2.0
    col_x = [body.x + 8, body.x + col_gap]
    col_i = 0
    c.setFillColor(HexColor("#222222"))
    c.setFont("Helvetica", 8)

    for i, item in enumerate(municipalities, start=1):
        cve = str(item.get("cve_mun") or "").strip()
        nom = str(item.get("nomgeo") or "").strip()
        line = f"{i:02d}.  {cve}  —  {nom}"[:58]
        if y < body.y + 12:
            if col_i == 0:
                col_i = 1
                y = body.y2 - 40
            else:
                c.setFont("Helvetica-Oblique", 7.5)
                c.setFillColor(HexColor("#666666"))
                c.drawString(
                    col_x[0],
                    body.y + 14,
                    "… listado truncado en portada (ver hojas siguientes)",
                )
                break
        c.setFillColor(HexColor("#222222"))
        c.setFont("Helvetica", 8)
        c.drawString(col_x[col_i], y, line)
        y -= 12

    draw_brand_footer(c, layout.footer, footer)


def render_pdf(
    *,
    layout: PageLayout,
    title: str,
    footer: str,
    geometry: Optional[BaseGeometry] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    symbol: Optional[PolygonSymbol] = None,
    layers: Optional[Sequence[LayerData]] = None,
    legend_items: Optional[Sequence[LegendItem]] = None,
    map_scale: Optional[float] = None,
    demo: bool = False,
    brand_subtitle: Optional[str] = None,
    labels: Optional[Sequence[dict]] = None,
    max_labels: int = 60,
    strip_content: Any = None,
    croquis_panel_content: Any = None,
    condensado_panel_content: Any = None,
    sip_features: Optional[Sequence[dict]] = None,
    cd_features: Optional[Sequence[dict]] = None,
) -> bytes:
    buf = BytesIO()
    c = Canvas(buf, pagesize=(layout.page_width, layout.page_height))
    draw_map_page(
        c,
        layout=layout,
        title=title,
        footer=footer,
        geometry=geometry,
        bounds=bounds,
        symbol=symbol,
        layers=layers,
        legend_items=legend_items,
        map_scale=map_scale,
        demo=demo,
        brand_subtitle=brand_subtitle,
        labels=labels,
        max_labels=max_labels,
        strip_content=strip_content,
        croquis_panel_content=croquis_panel_content,
        condensado_panel_content=condensado_panel_content,
        sip_features=sip_features,
        cd_features=cd_features,
    )
    c.showPage()
    c.save()
    return buf.getvalue()


def _layout_pagesize(layout: Any) -> tuple[float, float]:
    return (float(layout.page_width), float(layout.page_height))


def render_pdf_document(
    *,
    page_width: float,
    page_height: float,
    cover: Optional[dict[str, Any]] = None,
    map_pages: Sequence[dict[str, Any]],
) -> bytes:
    """Ensambla PDF. Cada map_page puede traer ``layout`` con distinto pagesize."""
    if not map_pages and not cover:
        raise ValueError("Documento PDF sin páginas")

    buf = BytesIO()
    # Tamaño inicial: portada, primera hoja de mapa, o fallback del caller
    if cover is not None and cover.get("layout") is not None:
        init_w, init_h = _layout_pagesize(cover["layout"])
    elif map_pages and map_pages[0].get("layout") is not None:
        init_w, init_h = _layout_pagesize(map_pages[0]["layout"])
    else:
        init_w, init_h = float(page_width), float(page_height)

    c = Canvas(buf, pagesize=(init_w, init_h))

    if cover:
        draw_cover_page(
            c,
            layout=cover["layout"],
            title=str(cover.get("title") or "Atlas municipal"),
            subtitle=str(cover.get("subtitle") or get_branding()["brand_line"]),
            municipalities=cover.get("municipalities") or [],
            footer=str(cover.get("footer") or ""),
        )
        c.showPage()
        if map_pages and map_pages[0].get("layout") is not None:
            c.setPageSize(_layout_pagesize(map_pages[0]["layout"]))

    for i, page in enumerate(map_pages):
        layout = page.get("layout")
        if layout is not None and (i > 0 or cover):
            c.setPageSize(_layout_pagesize(layout))
        elif layout is not None and i == 0 and not cover:
            # Canvas ya abrió con este tamaño; no-op seguro
            pass
        draw_map_page(c, **page)
        c.showPage()

    c.save()
    return buf.getvalue()


def padded_bounds(
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    pad_ratio: float = 0.08,
    min_pad: float = 0.0,
) -> tuple[float, float, float, float]:
    dx = max(maxx - minx, 1.0)
    dy = max(maxy - miny, 1.0)
    px = max(dx * pad_ratio, float(min_pad or 0.0))
    py = max(dy * pad_ratio, float(min_pad or 0.0))
    return minx - px, miny - py, maxx + px, maxy + py


def expand_bounds_to_frame_aspect(
    bounds: tuple[float, float, float, float],
    frame: Box,
) -> tuple[float, float, float, float]:
    """Expande el extent geográfico para igualar el aspect del ``map_frame``.

    ``world_to_page`` usa ``min(fw/dx, fh/dy)`` y centra: si el aspect del
    municipio+pad no coincide con el marco, quedan bandas vacías (letterbox)
    y el ctx parece “recortado” antes de las orillas. Expandir el eje corto
    no cambia el pad_ratio pedido: solo llena el marco visible.
    """
    minx, miny, maxx, maxy = bounds
    dx = max(maxx - minx, 1e-9)
    dy = max(maxy - miny, 1e-9)
    fw = max(float(frame.width), 1e-9)
    fh = max(float(frame.height), 1e-9)
    frame_aspect = fw / fh
    bounds_aspect = dx / dy
    if bounds_aspect > frame_aspect:
        # Demasiado ancho respecto al marco → crecer en Y (norte/sur).
        new_dy = dx / frame_aspect
        cy = (miny + maxy) / 2.0
        return minx, cy - new_dy / 2.0, maxx, cy + new_dy / 2.0
    if bounds_aspect < frame_aspect:
        # Demasiado alto → crecer en X (este/oeste).
        new_dx = dy * frame_aspect
        cx = (minx + maxx) / 2.0
        return cx - new_dx / 2.0, miny, cx + new_dx / 2.0, maxy
    return minx, miny, maxx, maxy


def build_page_layout(
    paper: str,
    orientation: str,
    legend_items: int = 0,
    spec=None,
) -> PageLayout:
    return build_layout(
        paper=paper,  # type: ignore[arg-type]
        orientation=orientation,  # type: ignore[arg-type]
        legend_items=legend_items,
        spec=spec,
    )


def symbol_from_template(template: dict[str, Any]) -> PolygonSymbol:
    style = (template.get("style") or {}).get("municipio") or template.get("symbol") or {}
    return polygon_symbol_from_dict(style if isinstance(style, dict) else {})
