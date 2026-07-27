"""Servicio principal: orquesta plantilla → datos → layout → PDF/SVG/GeoPDF."""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from cartography_engine import ENGINE_NAME, __version__
from cartography_engine.datasource import (
    MunicipalityRef,
    ageb_label_point,
    cd_features_from_geometry,
    fetch_ageb_clave_for_localidad,
    fetch_cd_labeled_points,
    fetch_labels_in_bbox,
    fetch_layers_in_bbox,
    fetch_localidad_area,
    fetch_locality,
    fetch_municipality,
    fetch_municipality_cartography,
    fetch_sip_points,
    fetch_state_extent,
    fetch_template_labels,
    fetch_template_layers,
    format_ageb_clave,
    list_municipalities,
)
from cartography_engine.pdf.strip import StripContent
from cartography_engine.symbols.legal_texts import ADVERTENCIA_TEXT
from cartography_engine.geopdf import GeoViewport, tag_pdf
from cartography_engine.layouts import LayoutSpec, parse_layout_spec, resolve_layout_spec
from cartography_engine.layouts import build_layout as build_layout_from_spec
from cartography_engine.layers import (
    legend_items_from_layers,
    legend_items_from_template,
    parse_layers_from_template,
)
from cartography_engine.models import CartographyError, GenerateMapRequest
from cartography_engine.pdf import (
    build_page_layout,
    expand_bounds_to_frame_aspect,
    padded_bounds,
    render_pdf,
    render_pdf_document,
)
from cartography_engine.renderers import compute_map_scale
from cartography_engine.svg import render_svg
from cartography_engine.symbols import PointSymbol, PolygonSymbol
from cartography_engine.templates_loader import list_template_ids, load_template
from cartography_engine.plu_multipage import (
    PLU_DETAIL_SCALE,
    PLU_DETAIL_VIAL_MAX,
    PLU_DETAIL_VIAL_SIZE,
    PLU_MAX_PAGES,
    clip_layers_to_bounds,
    filter_features_to_bounds,
    parse_assembly_package_param,
    parse_multipage_param,
    plan_plu_tile_grid,
    style_plu_detail_page_labels,
    vialidad_labels_in_bounds,
    want_plu_multipage,
)

log = logging.getLogger(__name__)

ATLAS_MAX_MAP_PAGES = 90


def health_payload() -> dict[str, Any]:
    from database import cartography_db_status

    carto = cartography_db_status()
    caps = [
        "layers",
        "legend",
        "svg_export",
        "qgis_symbol_import",
        "basic_labels",
        "label_collision",
        "multi_page_atlas",
        "plu_multipage",
        "plu_assembly_package",
        "custom_layouts",
        "geopdf",
        "grosig_localidad",
        "grosig_condensado",
        "grosig_croquis",
        "sip_icons",
    ]
    return {
        "engine": ENGINE_NAME,
        "version": __version__,
        "enabled": True,
        "templates": list_template_ids(),
        "formats": ["pdf", "svg", "geopdf"],
        "capabilities": caps,
        "cartography_db": carto,
    }


def _demo_legend_items():
    from cartography_engine.layers import LegendItem

    return [
        LegendItem(
            id="demo_poly",
            label="Área de prueba",
            kind="polygon",
            symbol=PolygonSymbol(
                fill_color="#D9E8F5",
                fill_opacity=0.6,
                stroke_color="#1F4E79",
                stroke_width=1.5,
            ),
        ),
        LegendItem(
            id="demo_pts",
            label="Puntos de prueba",
            kind="point",
            symbol=PointSymbol(fill_color="#C0392B", stroke_color="#FFFFFF", size=3.5),
        ),
    ]


def _emit(
    fmt: str,
    pdf_kwargs: dict[str, Any],
    base_name: str,
    *,
    geoviewports: Optional[Sequence[GeoViewport]] = None,
) -> tuple[bytes, str, str]:
    if fmt == "svg":
        svg_kwargs = {
            k: v
            for k, v in pdf_kwargs.items()
            if k
            not in (
                "strip_content",
                "croquis_panel_content",
                "condensado_panel_content",
                "sip_features",
                "cd_features",
            )
        }
        data = render_svg(**svg_kwargs)
        return data, f"{base_name}.svg", "image/svg+xml"
    data = render_pdf(**pdf_kwargs)
    if fmt == "geopdf":
        if not geoviewports:
            raise CartographyError(
                "GEOPDF_NEEDS_BOUNDS",
                "format=geopdf requiere bounds espaciales (no disponible en demo_blank)",
            )
        data = tag_pdf(data, geoviewports)
        return data, f"{base_name}_geo.pdf", "application/pdf"
    return data, f"{base_name}.pdf", "application/pdf"


def _resolve_max_labels(layer_defs, params: Optional[dict[str, Any]]) -> int:
    label_cap = sum(int(getattr(d, "label_limit", 0) or 0) for d in layer_defs if d.label_field)
    if not label_cap:
        label_cap = 60
    try:
        override = (params or {}).get("max_labels")
        if override is not None:
            label_cap = max(1, min(int(override), 50000))
    except (TypeError, ValueError):
        pass
    return max(1, min(int(label_cap), 50000))


def _layout_spec_for_request(
    template: dict[str, Any],
    request: GenerateMapRequest,
    params: Optional[dict[str, Any]] = None,
) -> LayoutSpec:
    """
    Plantilla + params.layout.
    paper/orientation del body solo pisan si el cliente los envía (no null).
    """
    p = params if params is not None else request.params
    both_omitted = request.paper is None and request.orientation is None
    if both_omitted:
        return resolve_layout_spec(template, params=p, prefer_template_page=True)
    spec = resolve_layout_spec(template, params=p, prefer_template_page=True)
    from dataclasses import replace

    updates = {}
    if request.paper is not None:
        updates["paper"] = request.paper
    if request.orientation is not None:
        updates["orientation"] = request.orientation
    return replace(spec, **updates) if updates else spec


def _maybe_geopdf_footer(footer: str, fmt: str) -> str:
    if fmt != "geopdf":
        return footer
    if "GeoPDF" in footer:
        return footer
    return f"{footer} · GeoPDF"


def _croquis_page_payload(
    *,
    template: dict[str, Any],
    cve_mun: str,
    spec: LayoutSpec,
    params: Optional[dict[str, Any]] = None,
    title_key: str = "title",
    footer_key: str = "footer",
    fmt: str = "pdf",
) -> dict[str, Any]:
    """Arma kwargs de una hoja croquis (PDF/SVG) para un municipio."""
    feature = fetch_municipality(cve_mun)
    layer_defs = parse_layers_from_template(template)
    legend = legend_items_from_layers(layer_defs)
    layout = build_page_layout(
        spec.paper,
        spec.orientation,
        legend_items=len(legend),
        spec=spec,
    )

    layers = fetch_template_layers(
        layer_defs,
        cve_mun=feature.cve_mun,
        clip_geom=feature.geometry,
    )
    labels = fetch_template_labels(layer_defs, cve_mun=feature.cve_mun)

    minx, miny, maxx, maxy = feature.geometry.bounds
    for layer in layers:
        if layer.definition.id == "municipio" and layer.geometry is not None:
            minx, miny, maxx, maxy = layer.geometry.bounds
            break
    bounds = padded_bounds(minx, miny, maxx, maxy, pad_ratio=0.08)
    scale = compute_map_scale(bounds, layout.map_frame)

    title_tpl = str(template.get(title_key) or template.get("title") or "Croquis municipal — {nomgeo}")
    title = title_tpl.format(nomgeo=feature.nomgeo, cve_mun=feature.cve_mun)
    footer_tpl = str(
        template.get(footer_key)
        or template.get("footer")
        or "GroSIG Cartography Engine · {cve_mun} · CRS {crs}"
    )
    footer = footer_tpl.format(
        nomgeo=feature.nomgeo,
        cve_mun=feature.cve_mun,
        crs=feature.crs,
        n_pages=1,
    )
    footer = _maybe_geopdf_footer(footer, fmt)

    return dict(
        layout=layout,
        title=title,
        footer=footer,
        bounds=bounds,
        layers=layers,
        legend_items=legend,
        map_scale=scale,
        brand_subtitle=f"Croquis municipal · {feature.nomgeo}",
        labels=labels,
        max_labels=_resolve_max_labels(layer_defs, params),
        _meta={"cve_mun": feature.cve_mun, "nomgeo": feature.nomgeo, "crs": feature.crs},
    )


def _viewport_from_page(
    *,
    page_index: int,
    layout,
    bounds: tuple[float, float, float, float],
    crs: str,
) -> GeoViewport:
    return GeoViewport(
        page_index=page_index,
        map_frame=layout.map_frame,
        bounds_xy=bounds,
        crs=crs,
        page_width=layout.page_width,
        page_height=layout.page_height,
    )


def _norm_cve_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",")]
        items = parts
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        raise CartographyError("INVALID_CVE_LIST", "params.cve_mun_list debe ser lista o cadena")

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        digits = "".join(ch for ch in item if ch.isdigit())
        if not digits:
            continue
        cve = digits[-3:].zfill(3) if len(digits) >= 3 else digits.zfill(3)
        if cve in seen:
            continue
        seen.add(cve)
        out.append(cve)
    return out


def _resolve_atlas_municipalities(params: dict[str, Any]) -> list[MunicipalityRef]:
    cve_list = _norm_cve_list(params.get("cve_mun_list"))
    scope = str(params.get("scope") or "").strip().lower()

    if cve_list:
        by_cve = {m.cve_mun: m for m in list_municipalities()}
        resolved: list[MunicipalityRef] = []
        missing: list[str] = []
        for cve in sorted(cve_list):
            if cve in by_cve:
                resolved.append(by_cve[cve])
            else:
                try:
                    feat = fetch_municipality(cve)
                    resolved.append(MunicipalityRef(cve_mun=feat.cve_mun, nomgeo=feat.nomgeo))
                except CartographyError:
                    missing.append(cve)
        if missing:
            raise CartographyError(
                "MUNICIPIO_NOT_FOUND",
                f"Municipios no encontrados: {', '.join(missing)}",
                status_code=404,
            )
        return resolved

    if scope in ("state", "all", "estado"):
        return list_municipalities()

    raise CartographyError(
        "MISSING_ATLAS_SCOPE",
        "Indique params.cve_mun_list o params.scope=\"state\"",
    )


def _generate_atlas_municipal(
    request: GenerateMapRequest,
    template: dict[str, Any],
) -> tuple[bytes, str, str]:
    fmt = (request.format or "pdf").lower()
    if fmt not in ("pdf", "geopdf"):
        raise CartographyError(
            "ATLAS_PDF_ONLY",
            "El atlas municipal multipágina solo admite format=pdf o geopdf",
            status_code=400,
        )

    params = request.params or {}
    municipalities = _resolve_atlas_municipalities(params)
    max_pages = int(template.get("max_map_pages") or ATLAS_MAX_MAP_PAGES)
    max_pages = max(1, min(max_pages, ATLAS_MAX_MAP_PAGES))
    if len(municipalities) > max_pages:
        raise CartographyError(
            "ATLAS_TOO_MANY_PAGES",
            f"Máximo {max_pages} hojas de mapa; solicitados {len(municipalities)}",
            status_code=400,
        )
    if not municipalities:
        raise CartographyError("ATLAS_EMPTY", "No hay municipios para el atlas", status_code=400)

    cover_flag = params.get("cover", True)
    if isinstance(cover_flag, str):
        cover_flag = cover_flag.strip().lower() not in ("0", "false", "no", "off")
    else:
        cover_flag = bool(cover_flag)

    spec = _layout_spec_for_request(template, request, params)

    map_pages: list[dict[str, Any]] = []
    viewports: list[GeoViewport] = []
    page_offset = 1 if cover_flag else 0

    for i, ref in enumerate(municipalities):
        payload = _croquis_page_payload(
            template=template,
            cve_mun=ref.cve_mun,
            spec=spec,
            params=params,
            title_key="page_title",
            footer_key="page_footer",
            fmt=fmt,
        )
        meta = payload.pop("_meta", {}) or {}
        bounds = payload.get("bounds")
        layout = payload["layout"]
        if bounds and fmt == "geopdf":
            viewports.append(
                _viewport_from_page(
                    page_index=page_offset + i,
                    layout=layout,
                    bounds=bounds,
                    crs=str(meta.get("crs") or "EPSG:32614"),
                )
            )
        map_pages.append(payload)

    cover = None
    layout0 = map_pages[0]["layout"]
    if cover_flag:
        cover_layout = build_page_layout(
            spec.paper, spec.orientation, legend_items=0, spec=spec
        )
        n_pages = len(map_pages) + 1
        cover_footer = str(
            template.get("footer")
            or "GroSIG Cartography Engine · Atlas municipal · {n_pages} hojas"
        ).format(n_pages=n_pages, n_map_pages=len(map_pages))
        cover_footer = _maybe_geopdf_footer(cover_footer, fmt)
        cover = {
            "layout": cover_layout,
            "title": str(template.get("cover_title") or template.get("title") or "Atlas municipal"),
            "subtitle": str(
                template.get("cover_subtitle") or "GroSIG Cartography Engine · croquis por municipio"
            ),
            "municipalities": [{"cve_mun": m.cve_mun, "nomgeo": m.nomgeo} for m in municipalities],
            "footer": cover_footer,
        }

    data = render_pdf_document(
        page_width=layout0.page_width,
        page_height=layout0.page_height,
        cover=cover,
        map_pages=map_pages,
    )
    n = len(municipalities)
    if fmt == "geopdf":
        data = tag_pdf(data, viewports)
        return data, f"atlas_municipal_{n}mun_geo.pdf", "application/pdf"
    return data, f"atlas_municipal_{n}mun.pdf", "application/pdf"


def generate_map(request: GenerateMapRequest) -> tuple[bytes, str, str]:
    """
    Genera PDF, SVG o GeoPDF vectorial.
    Returns: (bytes, filename, media_type)
    """
    template = load_template(request.template_id)
    product = str(template.get("product") or template.get("id") or request.template_id)
    fmt = (request.format or "pdf").lower()
    if fmt not in ("pdf", "svg", "geopdf"):
        raise CartographyError("INVALID_FORMAT", "format debe ser pdf, svg o geopdf")

    if product == "demo_blank" or request.template_id == "demo_blank":
        if fmt == "geopdf":
            raise CartographyError(
                "GEOPDF_NEEDS_BOUNDS",
                "demo_blank no tiene bounds espaciales; use croquis_municipal con format=geopdf",
            )
        legend = _demo_legend_items()
        tpl_legend = legend_items_from_template(template)
        if len(tpl_legend) >= 2:
            legend = tpl_legend
        spec = _layout_spec_for_request(template, request)
        layout = build_page_layout(
            spec.paper, spec.orientation, legend_items=len(legend), spec=spec
        )
        title = str(template.get("title") or "GroSIG Cartography Engine — Demo")
        footer = str(
            template.get("footer")
            or "Producto de prueba · salida vectorial · sin dependencia del visor"
        )
        kwargs = dict(
            layout=layout,
            title=title,
            footer=footer,
            demo=True,
            legend_items=legend,
            map_scale=float(template.get("demo_scale") or 50000),
            brand_subtitle="Producto de prueba · Roadmap 1.4",
        )
        return _emit(fmt, kwargs, "grosig_demo_blank")

    if product == "croquis_municipal" or request.template_id in (
        "croquis_municipal",
        "croquis_map_focus",
    ):
        cve = str((request.params or {}).get("cve_mun") or "").strip()
        if not cve:
            raise CartographyError("MISSING_CVE_MUN", "params.cve_mun es obligatorio")
        spec = _layout_spec_for_request(template, request)
        kwargs = _croquis_page_payload(
            template=template,
            cve_mun=cve,
            spec=spec,
            params=request.params,
            fmt=fmt,
        )
        meta = kwargs.pop("_meta", {}) or {}
        bounds = kwargs.get("bounds")
        layout = kwargs["layout"]
        base = f"{request.template_id}_{meta.get('cve_mun') or cve}"
        geoviewports = None
        if fmt == "geopdf" and bounds:
            geoviewports = [
                _viewport_from_page(
                    page_index=0,
                    layout=layout,
                    bounds=bounds,
                    crs=str(meta.get("crs") or "EPSG:32614"),
                )
            ]
        return _emit(fmt, kwargs, base, geoviewports=geoviewports)

    if product == "atlas_municipal" or request.template_id == "atlas_municipal":
        return _generate_atlas_municipal(request, template)

    if product == "plano_localidad" or request.template_id == "plano_localidad":
        return _generate_plano_localidad(request, template, fmt)

    if product == "condensado_estatal" or request.template_id == "condensado_estatal":
        return _generate_condensado_estatal(request, template, fmt)

    if (
        product == "grosig_croquis_municipal"
        or request.template_id == "grosig_croquis_municipal"
    ):
        return _generate_grosig_croquis(request, template, fmt)

    raise CartographyError(
        "UNSUPPORTED_PRODUCT",
        f"Producto no soportado: {product}",
        status_code=400,
    )


def _locality_is_urban(ambito: str) -> bool:
    a = str(ambito or "").strip().upper()
    if a.startswith("RURAL") or a.startswith("R"):
        return False
    if a.startswith("URBAN") or a.startswith("U"):
        return True
    return True


def _resolve_plano_localidad_template(feature) -> dict[str, Any]:
    """Router PLR/PLU: rural congelado vs urbano fino."""
    if _locality_is_urban(getattr(feature, "ambito", "")):
        return load_template("plano_localidad_urbana")
    return load_template("plano_localidad_rural")


def _generate_plano_localidad(
    request: GenerateMapRequest,
    template: dict[str, Any],
    fmt: str,
) -> tuple[bytes, str, str]:
    params = request.params or {}
    cve_mun = str(params.get("cve_mun") or "").strip()
    cve_loc = str(params.get("cve_loc") or "").strip()
    cve_ent = str(params.get("cve_ent") or "12").strip()
    if not cve_mun or not cve_loc:
        raise CartographyError(
            "MISSING_CVE_LOC",
            "params.cve_mun y params.cve_loc son obligatorios para plano_localidad",
        )

    feature = fetch_locality(cve_mun=cve_mun, cve_loc=cve_loc, cve_ent=cve_ent)
    template = _resolve_plano_localidad_template(feature)
    layer_defs = parse_layers_from_template(template)
    legend = legend_items_from_layers(layer_defs)
    spec = _layout_spec_for_request(template, request, params)
    layout = build_page_layout(
        spec.paper, spec.orientation, legend_items=len(legend), spec=spec
    )

    layers = fetch_template_layers(
        layer_defs,
        cve_mun=feature.cve_mun,
        cve_loc=feature.cve_loc,
        clip_geom=feature.geometry,
    )
    labels = fetch_template_labels(
        layer_defs, cve_mun=feature.cve_mun, cve_loc=feature.cve_loc
    )
    sip_features = fetch_sip_points(
        cve_mun=feature.cve_mun, cve_loc=feature.cve_loc, limit=2500
    )
    cd_features = fetch_cd_labeled_points(
        cve_mun=feature.cve_mun, cve_loc=feature.cve_loc, limit=2500
    )

    try:
        mun_feat = fetch_municipality_cartography(feature.cve_mun)
        _ = mun_feat  # municipio disponible si se necesita
    except CartographyError:
        pass

    index_loc = fetch_localidad_area(cve_mun=feature.cve_mun, cve_loc=feature.cve_loc)
    if index_loc is None:
        index_loc = feature.geometry

    minx, miny, maxx, maxy = feature.geometry.bounds
    pe_geom = None
    mza_geom = None
    cd_geom = None
    # Encuadre = SOLO localidad + PE (no abrir zoom por colindantes).
    # Las AGEB rurales que caigan dentro del marco se dibujan; las demás quedan fuera.
    for ld in layers:
        lid = getattr(getattr(ld, "definition", None), "id", "") or ""
        geom = getattr(ld, "geometry", None)
        if geom is None:
            continue
        if lid == "pe":
            pe_geom = geom
        elif lid == "manzanas":
            mza_geom = geom
        elif lid == "cd":
            cd_geom = geom
        if lid == "pe":
            try:
                if not geom.is_empty:
                    px0, py0, px1, py1 = geom.bounds
                    minx, miny = min(minx, px0), min(miny, py0)
                    maxx, maxy = max(maxx, px1), max(maxy, py1)
            except Exception:
                pass
    # PLU: pad corto para acercar el zoom a la localidad (ref. ~1:22k)
    pad = float(template.get("pad_ratio") or 0.06)
    if _locality_is_urban(feature.ambito):
        pad = min(pad, 0.045)
    bounds = padded_bounds(minx, miny, maxx, maxy, pad_ratio=pad)
    scale = compute_map_scale(bounds, layout.map_frame)

    # CD: no filtrar por bbox del marco (los 6 pts viven en el borde del PE).
    # El zoom sigue anclado a L+PE; puntos fuera del PDF simplemente no se ven.

    ambito = feature.ambito.upper() if feature.ambito else "URBANA"
    if not ambito.startswith("URBAN") and not ambito.startswith("RURAL"):
        ambito = "URBANA" if "U" in ambito[:1] else "RURAL"
    if ambito.lower().startswith("u"):
        ambito_title = "URBANA"
    else:
        ambito_title = "RURAL"

    title = str(template.get("title") or "PLANO DE LOCALIDAD {ambito}").format(
        ambito=ambito_title,
        nomgeo=feature.nomgeo,
        cve_mun=feature.cve_mun,
        cve_loc=feature.cve_loc,
    )
    footer = str(template.get("footer") or "").format(
        cve_mun=feature.cve_mun,
        cve_loc=feature.cve_loc,
        nomgeo=feature.nomgeo,
        crs=feature.crs,
    )
    footer = _maybe_geopdf_footer(footer or "GroSIG · plano localidad", fmt)

    ageb_clave = fetch_ageb_clave_for_localidad(
        cve_mun=feature.cve_mun, cve_loc=feature.cve_loc
    )
    manzana_clave = "000"
    for lab in labels:
        lid = str(lab.get("layer_id") or "")
        txt = str(lab.get("text") or "").strip()
        if not txt:
            continue
        if lid == "manzanas" and manzana_clave == "000":
            manzana_clave = txt
        if lid == "ageb" and not ageb_clave:
            ageb_clave = format_ageb_clave(txt) or txt

    # PLR (rural, 1 AGEB típico): un óvalo en hueco del PE.
    # PLU (urbano): conservar TODAS las etiquetas AGEB del datasource.
    if not _locality_is_urban(feature.ambito):
        labels = [lab for lab in labels if str(lab.get("layer_id") or "") != "ageb"]
        if ageb_clave:
            pt = ageb_label_point(pe_geom, mza_geom, feature.geometry)
            if pt is not None:
                labels.append(
                    {
                        "text": ageb_clave,
                        "geometry": pt,
                        "layer_id": "ageb",
                        "color": "#C62828",
                        "bold": True,
                        "size": 9.0,
                        "style": "ageb_oval",
                        "anchor": "center",
                    }
                )
    else:
        # Asegurar estilo óvalo + tamaño fino en PLU
        n_ageb = 0
        for lab in labels:
            lid = str(lab.get("layer_id") or "")
            if lid == "colindantes":
                lab["style"] = "ageb_oval"
                lab["color"] = lab.get("color") or "#C62828"
                lab["bold"] = True
                lab["anchor"] = "center"
                try:
                    sz = float(lab.get("size") or 0)
                except (TypeError, ValueError):
                    sz = 0.0
                if sz < 4.8:
                    lab["size"] = 5.2
                continue
            if lid != "ageb":
                continue
            n_ageb += 1
            lab["style"] = "ageb_oval"
            lab["color"] = lab.get("color") or "#C62828"
            lab["bold"] = False
            try:
                sz = float(lab.get("size") or 0)
            except (TypeError, ValueError):
                sz = 0.0
            if sz <= 0 or sz > 2.8:
                lab["size"] = 2.4
            lab["anchor"] = "center"
        if n_ageb < 2:
            # Fallback explícito si el template no trajo AGEB
            from cartography_engine.datasource import fetch_urban_ageb_labels

            extra = fetch_urban_ageb_labels(
                cve_mun=feature.cve_mun,
                cve_loc=feature.cve_loc,
                limit=2000,
                size=2.4,
                bold=False,
            )
            if extra:
                labels = [lab for lab in labels if str(lab.get("layer_id") or "") != "ageb"]
                labels.extend(extra)

    # CD: triángulo + cve_mza se dibujan vía cd_features
    if not cd_features and cd_geom is not None:
        cd_features = cd_features_from_geometry(cd_geom)
    labels = [lab for lab in labels if str(lab.get("layer_id") or "") != "cd"]

    is_urban = _locality_is_urban(feature.ambito)
    if parse_multipage_param(params) and not is_urban:
        raise CartographyError(
            "MULTIPAGE_URBAN_ONLY",
            "params.multipage solo aplica a localidades urbanas (PLU)",
            status_code=400,
        )

    if want_plu_multipage(is_urban=is_urban, params=params, template=template):
        return _emit_plu_multipage(
            feature=feature,
            template=template,
            fmt=fmt,
            params=params,
            layout=layout,
            spec=spec,
            legend=legend,
            layers=layers,
            labels=labels,
            sip_features=sip_features,
            cd_features=cd_features,
            pe_geom=pe_geom,
            index_loc=index_loc,
            title=title,
            footer=footer,
            ageb_clave=ageb_clave or "000-0",
            manzana_clave=manzana_clave,
            layer_defs=layer_defs,
            extent_bounds=(minx, miny, maxx, maxy),
        )

    strip = StripContent(
        titulo=title,
        entidad=f"{feature.ent_nomgeo} ({feature.cve_ent})",
        municipio=f"{feature.mun_nomgeo} ({feature.cve_mun})",
        localidad=f"{feature.nomgeo} ({feature.cve_loc})",
        escala=float(scale or 0),
        armado="1 de 1",
        advertencia=ADVERTENCIA_TEXT,
        index_mun=pe_geom,
        index_loc=index_loc,
        ageb_clave=ageb_clave or "000-0",
        manzana_clave=manzana_clave,
    )

    kwargs = dict(
        layout=layout,
        title=title,
        footer=footer,
        bounds=bounds,
        layers=layers,
        legend_items=legend if spec.legend_enabled else [],
        map_scale=scale,
        brand_subtitle=None,
        labels=labels,
        max_labels=_resolve_max_labels(layer_defs, params),
        strip_content=strip,
        sip_features=sip_features,
        cd_features=cd_features,
    )
    base = f"plano_localidad_{feature.cve_mun}_{feature.cve_loc}"
    geoviewports = None
    if fmt == "geopdf":
        geoviewports = [
            _viewport_from_page(
                page_index=0,
                layout=layout,
                bounds=bounds,
                crs=feature.crs,
            )
        ]
    return _emit(fmt, kwargs, base, geoviewports=geoviewports)


def _emit_plu_multipage(
    *,
    feature,
    template: dict[str, Any],
    fmt: str,
    params: dict[str, Any],
    layout,
    spec,
    legend,
    layers,
    labels,
    sip_features,
    cd_features,
    pe_geom,
    index_loc,
    title: str,
    footer: str,
    ageb_clave: str,
    manzana_clave: str,
    layer_defs,
    extent_bounds: tuple[float, float, float, float],
) -> tuple[bytes, str, str]:
    """Cartas de detalle PLU a escala fija ~1:7 500 (opt-in)."""
    if fmt == "svg":
        raise CartographyError(
            "MULTIPAGE_PDF_ONLY",
            "PLU multipágina solo admite format=pdf o geopdf",
            status_code=400,
        )

    from shapely.geometry import box as shapely_box
    import logging
    import time

    log = logging.getLogger("cartography_engine.plu_mp")
    t0 = time.perf_counter()

    target_scale = float(template.get("detail_scale") or PLU_DETAIL_SCALE)
    max_pages = int(template.get("max_map_pages") or PLU_MAX_PAGES)
    max_pages = max(1, min(max_pages, PLU_MAX_PAGES))

    # Grilla sobre la localidad (no PE): menos cartas vacías / zoom más cercano.
    mask = index_loc if index_loc is not None else feature.geometry
    extent_geom = mask if mask is not None else shapely_box(*extent_bounds)

    tiles = plan_plu_tile_grid(
        extent_geom,
        target_scale=target_scale,
        frame_width_pt=layout.map_frame.width,
        frame_height_pt=layout.map_frame.height,
        overlap_ratio=float(template.get("tile_overlap") or 0.05),
        mask=mask,
        max_pages=max_pages,
    )
    if not tiles:
        raise CartographyError(
            "MULTIPAGE_EMPTY_GRID",
            "No se pudo calcular la grilla de cartas para la localidad",
            status_code=500,
        )

    tile_bounds_list = [tuple(t["bounds"]) for t in tiles]
    n = len(tiles)
    log.info(
        "PLU multipage %s-%s: %s cartas @ 1:%s",
        feature.cve_mun,
        feature.cve_loc,
        n,
        int(target_scale),
    )

    # Calles: el overview PLU solo trae ~55 etiquetas (centro de vialidad).
    # En detalle hay que re-fetch amplio y reubicar el texto en el tramo del tile.
    from cartography_engine.datasource import fetch_vialidad_labels

    vial_all = fetch_vialidad_labels(
        cve_mun=feature.cve_mun,
        cve_loc=feature.cve_loc,
        limit=15000,
    )
    base_labels = [
        lab for lab in (labels or []) if str(lab.get("layer_id") or "") != "ejes"
    ]
    log.info(
        "PLU multipage vialidad: %s etiquetas (con trazo) para %s-%s",
        len(vial_all),
        feature.cve_mun,
        feature.cve_loc,
    )

    max_labels = _resolve_max_labels(layer_defs, params)
    # En detalle, no hace falta el tope enorme de overview
    max_labels = min(max_labels, 8000)
    legend_items = legend if spec.legend_enabled else []

    want_package = parse_assembly_package_param(params)
    map_pages: list[dict[str, Any]] = []
    viewports: list[GeoViewport] = []
    page_offset = 0

    if want_package:
        # Hoja 1: plotter 90×120 panorama + grilla (tiles siguen calculados con frame dcarta)
        asm_spec = parse_layout_spec(
            {
                "preset": "grosig_localidad",
                "paper": "plotter_90x120",
                "orientation": "landscape",
                "strip_ratio": 0.08,
            }
        )
        asm_layout = build_layout_from_spec(
            paper=asm_spec.paper,
            orientation=asm_spec.orientation,
            legend_items=0,
            spec=asm_spec,
        )
        # Bounds = unión de tiles (+ pad leve) para cubrir el mismo territorio que las cartas
        ux0 = min(b[0] for b in tile_bounds_list)
        uy0 = min(b[1] for b in tile_bounds_list)
        ux1 = max(b[2] for b in tile_bounds_list)
        uy1 = max(b[3] for b in tile_bounds_list)
        if index_loc is not None and not getattr(index_loc, "is_empty", True):
            try:
                lx0, ly0, lx1, ly1 = index_loc.bounds
                ux0, uy0 = min(ux0, lx0), min(uy0, ly0)
                ux1, uy1 = max(ux1, lx1), max(uy1, ly1)
            except Exception:
                pass
        asm_bounds = padded_bounds(ux0, uy0, ux1, uy1, pad_ratio=0.03)
        asm_scale = compute_map_scale(asm_bounds, asm_layout.map_frame)
        asm_title = f"{title} — ÍNDICE DE ARMADO ({n} CARTAS)"
        asm_footer = _maybe_geopdf_footer(
            f"{footer} · índice plotter 90×120 · {n} cartas", fmt
        )
        asm_strip = StripContent(
            titulo=asm_title,
            entidad=f"{feature.ent_nomgeo} ({feature.cve_ent})",
            municipio=f"{feature.mun_nomgeo} ({feature.cve_mun})",
            localidad=f"{feature.nomgeo} ({feature.cve_loc})",
            escala=float(asm_scale or 0),
            armado=f"Índice · {n} cartas",
            advertencia=ADVERTENCIA_TEXT,
            index_mun=pe_geom,
            index_loc=index_loc,
            ageb_clave=ageb_clave,
            manzana_clave=manzana_clave,
            index_tiles=tile_bounds_list,
            active_tile_index=-1,
        )
        # Índice: sin etiquetas de vialidad (en plotter salen enormes y
        # muchas calles urbanas se llaman "1", "2"… → "1 (01899)").
        # Solo AGEB/manzana/SIP ligeros, tipografía acotada.
        _skip_lab = frozenset({"cd", "ejes", "sil", "sil_canal", "sil_corriente", "sil_carretera"})
        asm_labels = []
        for lab in labels or []:
            lid = str(lab.get("layer_id") or "")
            if lid in _skip_lab or lid.startswith("sil_"):
                continue
            lab2 = dict(lab)
            try:
                sz = float(lab2.get("size") or 0)
            except (TypeError, ValueError):
                sz = 0.0
            if lid in ("manzanas", "manzana"):
                lab2["size"] = min(sz if sz > 0 else 2.4, 2.6)
            elif lid == "ageb" or str(lab2.get("style") or "") == "ageb_oval":
                lab2["size"] = min(sz if sz > 0 else 2.4, 3.2)
            elif lid == "colindantes":
                lab2["size"] = min(sz if sz > 0 else 4.0, 4.5)
            else:
                if sz <= 0 or sz > 5.0:
                    lab2["size"] = 3.0
            asm_labels.append(lab2)
        map_pages.append(
            dict(
                layout=asm_layout,
                title=asm_title,
                footer=asm_footer,
                bounds=asm_bounds,
                layers=layers,
                legend_items=[],
                map_scale=asm_scale,
                brand_subtitle=None,
                labels=asm_labels,
                max_labels=min(max(_resolve_max_labels(layer_defs, params), 800), 2500),
                strip_content=asm_strip,
                sip_features=sip_features,
                cd_features=cd_features,
                assembly_tiles=tile_bounds_list,
                label_page_cap=8.0,
            )
        )
        if fmt == "geopdf":
            viewports.append(
                _viewport_from_page(
                    page_index=0,
                    layout=asm_layout,
                    bounds=asm_bounds,
                    crs=feature.crs,
                )
            )
        page_offset = 1
        log.info(
            "PLU package %s-%s: hoja índice plotter 90x120 + %s cartas dcarta",
            feature.cve_mun,
            feature.cve_loc,
            n,
        )

    for tile in tiles:
        tb = tuple(tile["bounds"])
        k = int(tile["index"])
        page_scale = compute_map_scale(tb, layout.map_frame)
        # Preferir escala objetivo en tira si el cálculo queda cerca
        if abs(page_scale - target_scale) / target_scale < 0.12:
            page_scale = target_scale
        strip = StripContent(
            titulo=title,
            entidad=f"{feature.ent_nomgeo} ({feature.cve_ent})",
            municipio=f"{feature.mun_nomgeo} ({feature.cve_mun})",
            localidad=f"{feature.nomgeo} ({feature.cve_loc})",
            escala=float(page_scale or target_scale),
            armado=f"{k} de {n}",
            advertencia=ADVERTENCIA_TEXT,
            index_mun=pe_geom,
            index_loc=index_loc,
            ageb_clave=ageb_clave,
            manzana_clave=manzana_clave,
            index_tiles=tile_bounds_list,
            active_tile_index=k - 1,
        )
        page_layers = clip_layers_to_bounds(layers, tb, buffer_m=50.0)
        # Cada hoja es insumo independiente: vialidad se recalcula en el tramo del tile
        page_labels = filter_features_to_bounds(base_labels, tb)
        page_labels.extend(
            vialidad_labels_in_bounds(
                vial_all,
                tb,
                size=PLU_DETAIL_VIAL_SIZE,
                max_labels=PLU_DETAIL_VIAL_MAX,
                skip_ninguno=True,
                text_mode="tipo_nom",
            )
        )
        page_labels = style_plu_detail_page_labels(page_labels)
        map_pages.append(
            dict(
                layout=layout,
                title=title,
                footer=footer,
                bounds=tb,
                layers=page_layers,
                legend_items=legend_items,
                map_scale=page_scale,
                brand_subtitle=None,
                labels=page_labels,
                max_labels=max_labels,
                strip_content=strip,
                sip_features=filter_features_to_bounds(sip_features, tb),
                cd_features=filter_features_to_bounds(cd_features, tb),
            )
        )
        if fmt == "geopdf":
            viewports.append(
                _viewport_from_page(
                    page_index=page_offset + k - 1,
                    layout=layout,
                    bounds=tb,
                    crs=feature.crs,
                )
            )

    data = render_pdf_document(
        page_width=map_pages[0]["layout"].page_width,
        page_height=map_pages[0]["layout"].page_height,
        cover=None,
        map_pages=map_pages,
    )
    n_pages = len(map_pages)
    log.info(
        "PLU multipage %s-%s listo: %s págs%s, %.1fs, %.1f KB",
        feature.cve_mun,
        feature.cve_loc,
        n_pages,
        " (paquete)" if want_package else "",
        time.perf_counter() - t0,
        len(data) / 1024.0,
    )
    if want_package:
        base = f"plano_localidad_{feature.cve_mun}_{feature.cve_loc}_pkg{n}"
    else:
        base = f"plano_localidad_{feature.cve_mun}_{feature.cve_loc}_mp{n}"
    if fmt == "geopdf":
        data = tag_pdf(data, viewports)
        return data, f"{base}_geo.pdf", "application/pdf"
    return data, f"{base}.pdf", "application/pdf"


def _generate_condensado_estatal(
    request: GenerateMapRequest,
    template: dict[str, Any],
    fmt: str,
) -> tuple[bytes, str, str]:
    import logging
    import time

    from cartography_engine.pdf.condensado_panel import CondensadoPanelContent
    from cartography_engine.symbols.legal_texts import ADVERTENCIA_TEXT

    log = logging.getLogger("cartography_engine")
    t0 = time.perf_counter()
    feature = fetch_state_extent()
    layer_defs = parse_layers_from_template(template)
    legend = legend_items_from_layers(layer_defs)
    spec = _layout_spec_for_request(template, request)
    layout = build_page_layout(
        spec.paper, spec.orientation, legend_items=len(legend), spec=spec
    )
    # Solo bbox (sin clip_geom): el && envelope basta y evita intersection
    # Python cara sobre multipolígonos estatales. Path exclusivo condensado.
    minx, miny, maxx, maxy = feature.geometry.bounds
    state_bbox = (float(minx), float(miny), float(maxx), float(maxy))
    t_fetch0 = time.perf_counter()
    layers = fetch_template_layers(layer_defs, bbox=state_bbox)
    layers = _condensado_ensure_localidades_area(layers, state_bbox)
    layers = _condensado_trim_municipio_along_estado(layers)
    t_layers = time.perf_counter() - t_fetch0
    t_lab0 = time.perf_counter()
    labels = fetch_template_labels(layer_defs, bbox=state_bbox)
    t_labels = time.perf_counter() - t_lab0

    bounds = padded_bounds(minx, miny, maxx, maxy, pad_ratio=0.04)
    scale = compute_map_scale(bounds, layout.map_frame)
    title = str(template.get("title") or "CONDENSADO ESTATAL — {nomgeo}").format(
        nomgeo=feature.nomgeo, cve_mun=feature.cve_mun
    )
    footer = str(
        template.get("footer") or "GroSIG · condensado estatal · CRS {crs}"
    ).format(nomgeo=feature.nomgeo, crs=feature.crs, cve_mun=feature.cve_mun)
    footer = _maybe_geopdf_footer(footer, fmt)

    # Tope alto: ~70–90% urbanas + más hidrónimos a escala estatal.
    max_labels = min(_resolve_max_labels(layer_defs, request.params), 1100)

    panel = CondensadoPanelContent(
        titulo=title,
        entidad=str(feature.nomgeo or "Guerrero"),
        cve_ent=str(getattr(feature, "cve_ent", None) or feature.cve_mun or "12"),
        escala=float(scale or 0.0),
        advertencia=ADVERTENCIA_TEXT,
        index_geom=feature.geometry,
    )

    kwargs = dict(
        layout=layout,
        title="",  # título vive en el panel
        footer="",
        bounds=bounds,
        layers=layers,
        legend_items=legend,
        map_scale=scale,
        brand_subtitle=None,
        labels=labels,
        max_labels=max_labels,
        condensado_panel_content=panel,
    )
    geoviewports = None
    if fmt == "geopdf":
        geoviewports = [
            _viewport_from_page(page_index=0, layout=layout, bounds=bounds, crs=feature.crs)
        ]
    t_ren0 = time.perf_counter()
    out = _emit(fmt, kwargs, "condensado_estatal", geoviewports=geoviewports)
    t_render = time.perf_counter() - t_ren0
    log.info(
        "condensado %s: layers=%.1fs labels=%.1fs render=%.1fs total=%.1fs feats=%s labs=%s bytes=%s",
        feature.nomgeo,
        t_layers,
        t_labels,
        t_render,
        time.perf_counter() - t0,
        sum(int(getattr(L, "feature_count", 0) or 0) for L in layers),
        len(labels),
        len(out[0]),
    )
    by_fc = {
        str(getattr(getattr(L, "definition", None), "id", "") or ""): int(
            getattr(L, "feature_count", 0) or 0
        )
        for L in layers
    }
    log.info(
        "condensado capas: urbana=%s doble=%s dash=%s otra=%s cuerpos=%s corrientes=%s",
        by_fc.get("localidades_urbana", 0),
        by_fc.get("carreteras_doble", 0),
        by_fc.get("carreteras_dash", 0),
        by_fc.get("carreteras_otra", 0),
        by_fc.get("cuerpos", 0),
        by_fc.get("corrientes", 0),
    )
    return out


def _condensado_ensure_localidades_area(
    layers: Sequence[Any],
    bbox: tuple[float, float, float, float],
) -> list[Any]:
    """Si localidades_a quedó vacía (filtro ambito/WKB), refetch sin ambito."""
    import logging
    from dataclasses import replace as dc_replace

    from cartography_engine.datasource import fetch_layer
    from cartography_engine.layers import LayerData

    log = logging.getLogger("cartography_engine")
    out = list(layers)
    idx = None
    loc_layer = None
    for i, L in enumerate(out):
        lid = str(getattr(getattr(L, "definition", None), "id", "") or "")
        if lid == "localidades_urbana":
            idx = i
            loc_layer = L
            break
    if loc_layer is None:
        return out
    fc = int(getattr(loc_layer, "feature_count", 0) or 0)
    geom = getattr(loc_layer, "geometry", None)
    if fc > 0 and geom is not None and not getattr(geom, "is_empty", True):
        log.info("condensado localidades_a ok feats=%s", fc)
        return out

    defn = getattr(loc_layer, "definition", None)
    if defn is None:
        return out
    try:
        # Quitar filtro ambito y reintentar (todas las localidades de área en bbox).
        clean = dc_replace(
            defn,
            attr_filters=tuple(
                (c, v)
                for c, v in (getattr(defn, "attr_filters", ()) or ())
                if str(c).lower() != "ambito"
            ),
            limit=max(int(getattr(defn, "limit", 0) or 0), 5000),
            optional=True,
        )
        # type: ignore[arg-type] — LayerDef frozen
        data = fetch_layer(clean, bbox=bbox)
        n = int(getattr(data, "feature_count", 0) or 0)
        g = getattr(data, "geometry", None)
        log.info(
            "condensado localidades_a refetch sin ambito feats=%s empty=%s",
            n,
            g is None or getattr(g, "is_empty", True),
        )
        if n > 0 and g is not None and not getattr(g, "is_empty", True):
            # Conservar definición original (simbología/leyenda) con geometría nueva
            out[idx] = LayerData(
                definition=defn,
                geometry=g,
                feature_count=n,
            )
    except Exception:
        log.exception("condensado localidades_a refetch falló")
    return out


def _condensado_trim_municipio_along_estado(layers: Sequence[Any]) -> list[Any]:
    """Quita el trazo municipal donde coincide con el estatal (+++ rojo visible)."""
    from cartography_engine.layers import LayerData

    by_id = {
        str(getattr(getattr(L, "definition", None), "id", "") or ""): L for L in layers
    }
    mun = by_id.get("municipios_l")
    est = by_id.get("estados_l")
    if mun is None or est is None:
        return list(layers)
    mg = getattr(mun, "geometry", None)
    eg = getattr(est, "geometry", None)
    if mg is None or eg is None or getattr(mg, "is_empty", True) or getattr(eg, "is_empty", True):
        return list(layers)
    try:
        minx, miny, maxx, maxy = eg.bounds
        span = max(maxx - minx, maxy - miny, 1.0)
        # ~0.15–0.2 mm a ~1:450k ≈ decenas de metros
        tol = max(45.0, span * 0.00018)
        cutter = eg.buffer(tol)
        trimmed = mg.difference(cutter)
        if trimmed is None or getattr(trimmed, "is_empty", True):
            return list(layers)
        new_mun = LayerData(
            definition=mun.definition,
            geometry=trimmed,
            feature_count=int(getattr(mun, "feature_count", 0) or 0),
        )
        out: list[Any] = []
        for L in layers:
            lid = str(getattr(getattr(L, "definition", None), "id", "") or "")
            out.append(new_mun if lid == "municipios_l" else L)
        return out
    except Exception:
        logging.getLogger("cartography_engine").exception(
            "condensado: trim municipios_l vs estados_l falló"
        )
        return list(layers)


def _generate_grosig_croquis(
    request: GenerateMapRequest,
    template: dict[str, Any],
    fmt: str,
) -> tuple[bytes, str, str]:
    from cartography_engine.pdf.croquis_panel import CroquisPanelContent

    cve = str((request.params or {}).get("cve_mun") or "").strip()
    if not cve:
        raise CartographyError("MISSING_CVE_MUN", "params.cve_mun es obligatorio")

    feature = fetch_municipality_cartography(cve)
    layer_defs = parse_layers_from_template(template)
    _croquis_deferred = frozenset({"estados_l", "entidad"})
    focus_defs = [
        L
        for L in layer_defs
        if not str(L.id or "").startswith("ctx_") and str(L.id or "") not in _croquis_deferred
    ]
    ctx_defs = [L for L in layer_defs if str(L.id or "").startswith("ctx_")]
    legend = legend_items_from_layers(focus_defs)
    spec = _layout_spec_for_request(template, request)
    # Forzar caja de panel aunque la leyenda dinámica venga vacía
    layout = build_page_layout(
        spec.paper, spec.orientation, legend_items=max(1, len(legend)), spec=spec
    )
    layers = fetch_template_layers(
        focus_defs, cve_mun=feature.cve_mun, clip_geom=feature.geometry
    )
    layers = _croquis_ensure_localidades_area(
        layers, cve_mun=feature.cve_mun, clip_geom=feature.geometry
    )
    labels = fetch_template_labels(focus_defs, cve_mun=feature.cve_mun)

    minx, miny, maxx, maxy = feature.geometry.bounds
    # Encaje centrado en el municipio foco (pad_ratio=0.04).
    bounds = padded_bounds(minx, miny, maxx, maxy, pad_ratio=0.04)
    # Llenar el map_frame sin letterbox: el ctx usa el mismo extent visible.
    bounds = expand_bounds_to_frame_aspect(bounds, layout.map_frame)

    # Contexto vecinos: todo lo que intersecta el extent del mapa (margen).
    if ctx_defs:
        ctx_layers = fetch_layers_in_bbox(
            ctx_defs,
            bbox=bounds,
            focus_geom=feature.geometry,
            exclude_cve_mun=feature.cve_mun,
        )
        # Ctx vecinos: localidades de área + cuerpos + nombres mun.
        ctx_label_defs = [
            L
            for L in ctx_defs
            if L.id
            in (
                "ctx_localidades_a",
                "ctx_localidades_urbana",
                "ctx_localidades_rural",
                "ctx_municipios",
                "ctx_cuerpos",
                "ctx_corrientes",
            )
        ]
        ctx_labels = fetch_labels_in_bbox(
            ctx_label_defs,
            bbox=bounds,
            focus_geom=feature.geometry,
            exclude_cve_mun=feature.cve_mun,
        )
        # z-order: ctx debajo del municipio foco
        layers = list(ctx_layers) + list(layers)
        labels = list(labels or []) + list(ctx_labels or [])

    # Diagnóstico: localidades de área deben traer geometría (sin filtro ambito).
    for layer in layers:
        lid = str(getattr(getattr(layer, "definition", None), "id", "") or "")
        if lid not in (
            "localidades_urbana",
            "localidades_rural",
            "localidades_a",
            "ctx_localidades_a",
            "ctx_localidades_urbana",
            "ctx_localidades_rural",
        ):
            continue
        g = getattr(layer, "geometry", None)
        n = int(getattr(layer, "feature_count", 0) or 0)
        empty = g is None or getattr(g, "is_empty", True)
        log.info(
            "croquis localidades area layer=%s features=%s empty=%s geom=%s",
            lid,
            n,
            empty,
            getattr(g, "geom_type", None) if g is not None else None,
        )

    layers, labels = _croquis_drop_points_covered_by_areas(layers, labels)
    layers, labels = _croquis_mask_ageb_under_localidades(layers, labels)
    labels = _croquis_thin_corriente_labels(labels)

    # Límite estatal + etiqueta de entidad: por extent completo (sin restar el foco).
    from cartography_engine.datasource import fetch_layer, fetch_layer_labels

    for L in layer_defs:
        lid = str(L.id or "")
        if lid == "estados_l":
            try:
                data = fetch_layer(L, bbox=bounds)
                if data is not None:
                    layers = list(layers) + [data]
            except Exception:
                log.exception("croquis estados_l falló")
        elif lid == "entidad" and L.label_field:
            try:
                labs = fetch_layer_labels(L, bbox=bounds)
                if labs:
                    labels = list(labels or []) + list(labs)
            except Exception:
                log.exception("croquis entidad labels falló")

    scale = compute_map_scale(bounds, layout.map_frame)
    title = str(
        template.get("title") or "CROQUIS MUNICIPAL CON MARCO GEOESTADÍSTICO"
    ).format(nomgeo=feature.nomgeo, cve_mun=feature.cve_mun)
    footer = str(
        template.get("footer") or "GroSIG · croquis municipal · {cve_mun} · CRS {crs}"
    ).format(nomgeo=feature.nomgeo, cve_mun=feature.cve_mun, crs=feature.crs)
    footer = _maybe_geopdf_footer(footer, fmt)

    panel = CroquisPanelContent(
        titulo=title,
        entidad=str(feature.ent_nomgeo or "Guerrero"),
        cve_ent=str(feature.cve_ent or "12"),
        municipio=str(feature.nomgeo or ""),
        cve_mun=str(feature.cve_mun or ""),
        escala=float(scale or 0.0),
        advertencia=ADVERTENCIA_TEXT,
        index_geom=feature.geometry,
    )

    kwargs = dict(
        layout=layout,
        title="",  # título vive en el panel derecho
        footer="",
        bounds=bounds,
        layers=layers,
        legend_items=legend,
        map_scale=scale,
        brand_subtitle=None,
        labels=labels,
        max_labels=_resolve_max_labels(layer_defs, request.params),
        croquis_panel_content=panel,
    )
    base = f"grosig_croquis_{feature.cve_mun}"
    geoviewports = None
    if fmt == "geopdf":
        geoviewports = [
            _viewport_from_page(page_index=0, layout=layout, bounds=bounds, crs=feature.crs)
        ]
    return _emit(fmt, kwargs, base, geoviewports=geoviewports)


def _croquis_ensure_localidades_area(
    layers: Sequence[Any],
    *,
    cve_mun: str,
    clip_geom: Any = None,
) -> list[Any]:
    """Si urbana/rural quedaron vacías (filtro ambito), refetch sin ambito.

    Misma red de seguridad que el condensado: en BD el ámbito suele ser 1/2,
    no 'Urbana'/'Rural'. Mejor pintar todas las de área que dejar el mapa vacío.
    """
    import logging
    from dataclasses import replace as dc_replace

    from cartography_engine.datasource import fetch_layer
    from cartography_engine.layers import LayerData

    log = logging.getLogger("cartography_engine")
    out = list(layers)
    by_idx = {
        str(getattr(getattr(L, "definition", None), "id", "") or ""): i
        for i, L in enumerate(out)
    }

    def _empty(L: Any) -> bool:
        g = getattr(L, "geometry", None)
        n = int(getattr(L, "feature_count", 0) or 0)
        return n <= 0 or g is None or getattr(g, "is_empty", True)

    need_u = "localidades_urbana" in by_idx and _empty(out[by_idx["localidades_urbana"]])
    need_r = "localidades_rural" in by_idx and _empty(out[by_idx["localidades_rural"]])
    if not need_u and not need_r:
        return out

    base_id = "localidades_urbana" if "localidades_urbana" in by_idx else "localidades_rural"
    defn = getattr(out[by_idx[base_id]], "definition", None)
    if defn is None:
        return out
    try:
        clean = dc_replace(
            defn,
            id="_croquis_localidades_a_all",
            attr_filters=tuple(
                (c, v)
                for c, v in (getattr(defn, "attr_filters", ()) or ())
                if str(c).lower() != "ambito"
            ),
            limit=max(int(getattr(defn, "limit", 0) or 0), 3000),
            optional=True,
        )
        data = fetch_layer(clean, cve_mun=cve_mun, clip_geom=None)
        n = int(getattr(data, "feature_count", 0) or 0)
        g = getattr(data, "geometry", None)
        empty = g is None or getattr(g, "is_empty", True)
        log.info(
            "croquis localidades_a refetch sin ambito feats=%s empty=%s need_u=%s need_r=%s",
            n,
            empty,
            need_u,
            need_r,
        )
        if n <= 0 or empty:
            return out
        # Si urbana vacía → rellenar (prioridad visual). Rural solo si urbana ya tenía datos.
        if need_u:
            u_def = out[by_idx["localidades_urbana"]].definition
            out[by_idx["localidades_urbana"]] = LayerData(
                definition=u_def, geometry=g, feature_count=n
            )
        elif need_r:
            r_def = out[by_idx["localidades_rural"]].definition
            out[by_idx["localidades_rural"]] = LayerData(
                definition=r_def, geometry=g, feature_count=n
            )
    except Exception:
        log.exception("croquis ensure localidades_a falló")
    return out


def _croquis_drop_points_covered_by_areas(
    layers: Sequence[Any],
    labels: Sequence[dict[str, Any]] | None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Si hay polígono de localidad amanzanada, no dibujar el punto homólogo."""
    from shapely.geometry import GeometryCollection, MultiPoint, Point
    from shapely.ops import unary_union

    from cartography_engine.layers import LayerData

    area_geoms = []
    for layer in layers:
        lid = str(getattr(getattr(layer, "definition", None), "id", "") or "")
        if lid not in (
            "localidades_urbana",
            "localidades_rural",
            "ctx_localidades_a",
            "ctx_localidades_urbana",
            "ctx_localidades_rural",
        ):
            continue
        g = getattr(layer, "geometry", None)
        if g is not None and not getattr(g, "is_empty", True):
            area_geoms.append(g)
    if not area_geoms:
        return list(layers), list(labels or [])

    try:
        cover = unary_union(area_geoms).buffer(25.0)
    except Exception:
        cover = unary_union(area_geoms)

    def _keep_point(pt: Point) -> bool:
        try:
            return not cover.covers(pt)
        except Exception:
            return True

    new_layers: list[Any] = []
    for layer in layers:
        lid = str(getattr(getattr(layer, "definition", None), "id", "") or "")
        if lid != "localidades_p" and lid != "ctx_localidades_p":
            new_layers.append(layer)
            continue
        geom = getattr(layer, "geometry", None)
        if geom is None or getattr(geom, "is_empty", True):
            new_layers.append(layer)
            continue
        kept: list[Point] = []
        try:
            if isinstance(geom, Point):
                pts = [geom]
            elif isinstance(geom, MultiPoint):
                pts = list(geom.geoms)
            elif isinstance(geom, GeometryCollection):
                pts = [p for p in geom.geoms if isinstance(p, Point)]
            else:
                pts = [
                    p
                    for p in getattr(geom, "geoms", [geom])
                    if isinstance(p, Point)
                ]
            for p in pts:
                if _keep_point(p):
                    kept.append(p)
        except Exception:
            new_layers.append(layer)
            continue
        if not kept:
            new_layers.append(
                LayerData(definition=layer.definition, geometry=None, feature_count=0)
            )
        elif len(kept) == 1:
            new_layers.append(
                LayerData(
                    definition=layer.definition,
                    geometry=kept[0],
                    feature_count=1,
                )
            )
        else:
            new_layers.append(
                LayerData(
                    definition=layer.definition,
                    geometry=MultiPoint(kept),
                    feature_count=len(kept),
                )
            )

    new_labels: list[dict[str, Any]] = []
    for lab in labels or []:
        if str(lab.get("layer_id") or "") not in ("localidades_p", "ctx_localidades_p"):
            new_labels.append(lab)
            continue
        g = lab.get("geometry")
        if g is None:
            continue
        try:
            if isinstance(g, Point):
                pt = g
            else:
                pt = g.representative_point()
            if _keep_point(pt):
                new_labels.append(lab)
        except Exception:
            new_labels.append(lab)
    return new_layers, new_labels


def _croquis_thin_corriente_labels(
    labels: Sequence[dict[str, Any]] | None,
    *,
    min_separation: float = 3500.0,
    max_per_name: int = 2,
) -> list[dict[str, Any]]:
    """Evita repetir el mismo nombre de río (foco+ctx) en tramos cercanos."""
    if not labels:
        return []
    hydro_ids = frozenset({"corrientes", "ctx_corrientes"})
    other: list[dict[str, Any]] = []
    by_name: dict[str, list[dict[str, Any]]] = {}
    for lab in labels:
        lid = str(lab.get("layer_id") or "")
        if lid not in hydro_ids:
            other.append(lab)
            continue
        key = str(lab.get("text") or "").strip().upper()
        if not key:
            continue
        by_name.setdefault(key, []).append(lab)

    kept_hydro: list[dict[str, Any]] = []
    for _key, group in by_name.items():
        # Preferir los de geometría más “central”; orden estable por aparición
        selected: list[dict[str, Any]] = []
        for lab in group:
            if len(selected) >= max_per_name:
                break
            g = lab.get("geometry")
            if g is None:
                continue
            try:
                pt = g if g.geom_type == "Point" else g.representative_point()
            except Exception:
                continue
            too_close = False
            for prev in selected:
                pg = prev.get("geometry")
                if pg is None:
                    continue
                try:
                    ppt = pg if pg.geom_type == "Point" else pg.representative_point()
                    if float(pt.distance(ppt)) < float(min_separation):
                        too_close = True
                        break
                except Exception:
                    continue
            if too_close:
                continue
            selected.append(lab)
        if not selected and group:
            selected = [group[0]]
        kept_hydro.extend(selected)
    return other + kept_hydro


def _croquis_mask_ageb_under_localidades(
    layers: Sequence[Any],
    labels: Sequence[dict[str, Any]] | None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Quita trazo/etiquetas AGEB rural donde hay localidad de área (evita amontonamiento)."""
    from shapely.geometry import Point
    from shapely.ops import unary_union

    from cartography_engine.layers import LayerData

    area_ids = (
        "localidades_urbana",
        "localidades_rural",
        "localidades_a",
        "ctx_localidades_a",
        "ctx_localidades_urbana",
        "ctx_localidades_rural",
    )
    area_geoms = []
    for layer in layers:
        lid = str(getattr(getattr(layer, "definition", None), "id", "") or "")
        if lid not in area_ids:
            continue
        g = getattr(layer, "geometry", None)
        if g is not None and not getattr(g, "is_empty", True):
            area_geoms.append(g)
    if not area_geoms:
        return list(layers), list(labels or [])

    try:
        cover = unary_union(area_geoms)
        # Buffer ligero (m en CRS mapa) para cubrir el dash naranja bajo el contorno.
        cover = cover.buffer(25.0)
    except Exception:
        try:
            cover = unary_union(area_geoms)
        except Exception:
            return list(layers), list(labels or [])

    new_layers: list[Any] = []
    for layer in layers:
        lid = str(getattr(getattr(layer, "definition", None), "id", "") or "")
        if lid != "ageb_rural":
            new_layers.append(layer)
            continue
        geom = getattr(layer, "geometry", None)
        if geom is None or getattr(geom, "is_empty", True):
            new_layers.append(layer)
            continue
        try:
            clipped = geom.difference(cover)
        except Exception:
            new_layers.append(layer)
            continue
        if clipped is None or getattr(clipped, "is_empty", True):
            new_layers.append(
                LayerData(definition=layer.definition, geometry=None, feature_count=0)
            )
        else:
            new_layers.append(
                LayerData(
                    definition=layer.definition,
                    geometry=clipped,
                    feature_count=int(getattr(layer, "feature_count", 0) or 0),
                )
            )

    new_labels: list[dict[str, Any]] = []
    for lab in labels or []:
        if str(lab.get("layer_id") or "") != "ageb_rural":
            new_labels.append(lab)
            continue
        g = lab.get("geometry")
        if g is None:
            continue
        try:
            pt = g if isinstance(g, Point) else g.representative_point()
            if cover.contains(pt) or cover.covers(pt):
                continue
            new_labels.append(lab)
        except Exception:
            new_labels.append(lab)
    return new_layers, new_labels
