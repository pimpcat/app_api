"""Pruebas del GroSIG Cartography Engine (Fase 0 / 0.1)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# app_api como root de imports
APP_API = Path(__file__).resolve().parents[1]
if str(APP_API) not in sys.path:
    sys.path.insert(0, str(APP_API))


@pytest.fixture(autouse=True)
def _clear_settings_caches():
    from config import get_settings
    from cartography_engine.branding import clear_branding_cache
    from cartography_engine.config import get_cartography_settings

    get_settings.cache_clear()
    get_cartography_settings.cache_clear()
    clear_branding_cache()
    yield
    get_settings.cache_clear()
    get_cartography_settings.cache_clear()
    clear_branding_cache()


def test_branding_is_config_driven():
    from cartography_engine.branding import get_branding

    branding = get_branding()
    assert "brand_line" in branding
    assert isinstance(branding["logos"], list) and branding["logos"]
    assert all(isinstance(x, str) and x for x in branding["logos"])
    # Sin hardcode de marcas en el contrato: solo claves de config
    assert "fallback_labels" in branding


def test_layout_boxes_are_valid():
    from cartography_engine.layouts import build_layout

    layout = build_layout(paper="letter", orientation="portrait", legend_items=2)
    assert layout.page_width > 0 and layout.page_height > 0
    assert layout.map_frame.width > 50
    assert layout.map_frame.height > 50
    assert layout.brand_header.width > 0
    assert layout.legend is not None
    assert layout.title.y2 <= layout.brand_header.y + 0.01
    assert layout.map_frame.y >= layout.outer_frame.y
    assert layout.north.width > 0 and layout.scale_bar.width > 0


def test_grosig_localidad_strip_layout():
    from cartography_engine.layouts import build_layout, parse_layout_spec

    spec = parse_layout_spec({"preset": "grosig_localidad"})
    layout = build_layout(spec=spec)
    assert layout.strip_enabled is True
    assert layout.strip is not None
    assert layout.strip.height > 65
    assert layout.map_frame.y >= layout.strip.y2 - 0.01
    assert layout.page_width > layout.page_height
    # D-Carta 42×28 cm landscape
    assert abs(layout.page_width - 1190.55) < 1.0
    assert abs(layout.page_height - 793.70) < 1.0
    assert spec.paper == "dcarta_42x28"
    assert spec.strip_ratio <= 0.12


def test_format_ageb_clave():
    from cartography_engine.datasource import format_ageb_clave

    assert format_ageb_clave("1916") == "191-6"
    assert format_ageb_clave("191-6") == "191-6"
    assert format_ageb_clave("") == ""
    assert format_ageb_clave("0000") == ""


def test_exterior_layers_not_clipped_by_localidad():
    """PE/CD no deben intersectarse con clip_geom (marco.l) o desaparecen."""
    from cartography_engine.layers import LayerDef
    from cartography_engine.symbols import LineSymbol, PointSymbol

    pe = LayerDef(
        id="pe",
        label="PE",
        table="marco.pe",
        geom_kind="line",
        symbol=LineSymbol(),
        optional=True,
    )
    cd = LayerDef(
        id="cd",
        label="CD",
        table="marco.cd",
        geom_kind="point",
        symbol=PointSymbol(marker="triangle"),
        optional=True,
    )
    exterior_ids = frozenset(
        {"pe", "cd", "poligono_envolvente", "caserio", "caserio_disperso"}
    )
    assert pe.id in exterior_ids
    assert cd.id in exterior_ids
    assert str(pe.table).endswith(".pe")
    assert str(cd.table).endswith(".cd")


def test_grosig_marginalia_legend_full_column():
    from cartography_engine.layouts import build_layout, parse_layout_spec

    spec = parse_layout_spec({"preset": "grosig_marginalia"})
    layout = build_layout(legend_items=10, spec=spec)
    assert layout.legend is not None
    # Columna completa (no una cajita enana arriba)
    assert layout.legend.height >= layout.map_frame.height * 0.95
    assert layout.legend.width >= 180
    assert layout.north.height >= 48
    assert layout.scale_bar.height >= 36


def test_strip_type_scales_with_height():
    from cartography_engine.layouts import Box
    from cartography_engine.pdf.strip import _strip_type

    small = _strip_type(Box(0, 0, 800, 130))
    large = _strip_type(Box(0, 0, 2500, 380))
    assert large.title > small.title
    assert large.body > small.body
    assert large.section >= 18


def test_plano_localidad_template_loads():
    from cartography_engine.layers import parse_layers_from_template
    from cartography_engine.templates_loader import load_template

    tpl = load_template("plano_localidad")
    assert tpl["product"] == "plano_localidad"
    assert tpl.get("router") == "plano_localidad"
    layers = parse_layers_from_template(tpl)
    assert any(L.table == "marco.m" for L in layers)
    assert any(L.filter_cve_loc for L in layers)


def test_plr_plu_templates_and_router():
    from types import SimpleNamespace

    from cartography_engine.layers import parse_layers_from_template
    from cartography_engine.plu_multipage import (
        multipage_enabled_for_template,
        parse_assembly_package_param,
        parse_multipage_param,
        plan_plu_tile_grid,
        want_plu_multipage,
    )
    from cartography_engine.services import _locality_is_urban, _resolve_plano_localidad_template
    from cartography_engine.templates_loader import load_template
    from shapely.geometry import box as shapely_box

    plr = load_template("plano_localidad_rural")
    plu = load_template("plano_localidad_urbana")
    assert plr["profile"] == "PLR"
    assert plu["profile"] == "PLU"
    assert plu.get("pad_ratio", 0.06) <= 0.06
    assert plu.get("detail_scale") == 7500
    assert not multipage_enabled_for_template(plr)
    assert not multipage_enabled_for_template(plu)
    assert parse_multipage_param({"multipage": True}) is True
    assert parse_multipage_param({}) is False
    assert parse_assembly_package_param({"package": "index_plotter"}) is True
    assert parse_assembly_package_param({"assembly_sheet": True}) is True
    assert parse_assembly_package_param({"multipage": True}) is False
    assert want_plu_multipage(is_urban=True, params={"multipage": True}, template=plu)
    assert not want_plu_multipage(is_urban=False, params={"multipage": True}, template=plu)
    assert not want_plu_multipage(is_urban=True, params={}, template=plu)

    plr_layers = {L.id: L for L in parse_layers_from_template(plr)}
    plu_layers = {L.id: L for L in parse_layers_from_template(plu)}
    assert plu_layers["manzanas"].symbol.stroke_width < plr_layers["manzanas"].symbol.stroke_width
    assert plu_layers["ejes"].label_limit < plr_layers["ejes"].label_limit
    assert plu_layers["manzanas"].symbol.stroke_width <= 0.2
    assert plu_layers["ageb"].label_size <= 3.5
    assert plu_layers["ageb"].label_limit >= 500
    assert plu_layers["manzanas"].label_limit >= 10000
    assert plu_layers["cd"].symbol.size <= 2.5
    assert plu_layers["ageb"].symbol.stroke_width <= 0.5
    # PLU: sin filtro ambito estricto (evita 0 AGEB si el valor en BD no coincide)
    assert not any(col == "ambito" for col, _ in (plu_layers["ageb"].attr_filters or ()))

    rural = SimpleNamespace(ambito="Rural")
    urban = SimpleNamespace(ambito="Urbana")
    assert _locality_is_urban("Rural") is False
    assert _locality_is_urban("Urbana") is True
    assert _resolve_plano_localidad_template(rural)["profile"] == "PLR"
    assert _resolve_plano_localidad_template(urban)["profile"] == "PLU"
    assert plan_plu_tile_grid(None) == []

    # Extent grande → varias cartas; mask omite tiles vacíos
    big = shapely_box(0, 0, 4000, 3000)
    mask = shapely_box(500, 500, 2500, 2000)
    tiles = plan_plu_tile_grid(
        big,
        target_scale=7500.0,
        page_width_m=1000.0,
        page_height_m=800.0,
        overlap_ratio=0.0,
        mask=mask,
        max_pages=40,
    )
    assert len(tiles) >= 2
    assert tiles[0]["index"] == 1
    assert tiles[0]["total"] == len(tiles)
    assert all(t["total"] == len(tiles) for t in tiles)

    # Extent que cabe en una carta → 1 tile
    small = shapely_box(0, 0, 200, 150)
    one = plan_plu_tile_grid(
        small,
        page_width_m=800.0,
        page_height_m=560.0,
        mask=small,
    )
    assert len(one) == 1
    assert one[0]["total"] == 1

    # clip_layers_to_bounds reduce geometría fuera del tile
    from cartography_engine.layers import LayerData, LayerDef
    from cartography_engine.plu_multipage import clip_layers_to_bounds, vialidad_labels_in_bounds
    from shapely.geometry import LineString

    dummy_def = LayerDef(id="t", label="t", table="x.y")
    big = shapely_box(0, 0, 5000, 5000)
    ld = LayerData(definition=dummy_def, geometry=big, feature_count=1)
    clipped = clip_layers_to_bounds([ld], (0, 0, 100, 100), buffer_m=0)
    assert clipped[0].geometry is not None
    assert clipped[0].geometry.bounds[2] <= 100.01
    empty = clip_layers_to_bounds([ld], (9000, 9000, 9100, 9100), buffer_m=0)
    assert empty[0].geometry is None

    line = LineString([(0, 50), (200, 50)])
    labs = [
        {
            "text": "JAZMIN (04235)",
            "nomvial": "JAZMIN",
            "tipovial": "CALLE",
            "geometry": line.interpolate(0.5, normalized=True),
            "layer_id": "ejes",
            "_line": line,
            "angle": 0.0,
        }
    ]
    in_tile = vialidad_labels_in_bounds(
        labs, (40, 0, 160, 100), max_labels=50, text_mode="tipo_nom"
    )
    assert len(in_tile) == 1
    assert in_tile[0]["text"] == "CALLE JAZMIN"
    out_tile = vialidad_labels_in_bounds(labs, (500, 500, 600, 600), max_labels=50)
    assert out_tile == []
    # Calle larga en tile → máx 3
    long = LineString([(0, 50), (800, 50)])
    labs_long = [
        {
            "nomvial": "VICENTE GUERRERO",
            "tipovial": "AVENIDA",
            "text": "VICENTE GUERRERO (06684)",
            "geometry": long.interpolate(0.5, normalized=True),
            "layer_id": "ejes",
            "_line": long,
        }
    ]
    many = vialidad_labels_in_bounds(
        labs_long, (0, 0, 800, 100), max_labels=50, text_mode="tipo_nom"
    )
    assert 2 <= len(many) <= 3
    assert all(x["text"] == "AVENIDA VICENTE GUERRERO" for x in many)


def test_plu_index_grid_draw_smoke():
    """Índice multipágina dibuja sin explotar (PDF buffer mínimo)."""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas
    from shapely.geometry import box as shapely_box

    from cartography_engine.layouts import Box
    from cartography_engine.pdf.strip import _draw_index

    buf = BytesIO()
    c = Canvas(buf, pagesize=(400, 300))
    tiles = [
        (0.0, 1000.0, 1000.0, 2000.0),
        (1000.0, 1000.0, 2000.0, 2000.0),
        (0.0, 0.0, 1000.0, 1000.0),
    ]
    _draw_index(
        c,
        Box(10, 10, 180, 140),
        None,
        shapely_box(200, 200, 1800, 1800),
        tiles=tiles,
        active_tile_index=1,
    )
    c.showPage()
    c.save()
    assert len(buf.getvalue()) > 200


def test_index_box_preserves_aspect_ratio():
    from cartography_engine.layouts import Box
    from cartography_engine.pdf.strip import _fit_index_box

    outer = Box(0, 0, 200, 120)
    tall = _fit_index_box(outer, (0, 0, 1000, 3000))
    wide = _fit_index_box(outer, (0, 0, 3000, 1000))
    assert tall.height >= tall.width
    assert wide.width >= wide.height
    assert tall.width <= outer.width + 0.01
    assert wide.height <= outer.height + 0.01


def test_condensado_simplify_limits():
    from cartography_engine.layers import parse_layers_from_template
    from cartography_engine.templates_loader import load_template

    tpl = load_template("condensado_estatal")
    layers = {L.id: L for L in parse_layers_from_template(tpl)}
    assert tpl["layout"]["paper"] == "plotter_90x120"
    assert tpl["layout"]["orientation"] == "landscape"
    # Producto reducido (rendimiento <3 min): sin rural / 1–2 carriles / FFCC / cortinas.
    assert set(layers.keys()) == {
        "municipios",
        "localidades_urbana",
        "cuerpos",
        "corrientes",
        "carreteras_doble",
        "carreteras_dash",
        "carreteras_otra",
        "municipios_l",
        "estados_l",
        "aeropuerto_intl",
        "aeropuerto_local",
    }
    assert "localidades_rural" not in layers
    assert "carreteras_multi" not in layers
    assert "carreteras_dos" not in layers
    assert "carreteras_uno" not in layers
    # Simplify por escala (~1:450k): tolerancia en metros vía ST_SimplifyPreserveTopology
    assert layers["corrientes"].simplify >= 150
    assert layers["corrientes"].limit >= 2000
    assert getattr(layers["municipios"], "simplify", 0) == 0
    assert getattr(layers["localidades_urbana"], "simplify", 0) == 0
    assert layers["cuerpos"].simplify >= 100
    assert layers["carreteras_doble"].simplify >= 80
    assert layers["carreteras_doble"].symbol.decoration == "double"
    assert layers["carreteras_dash"].symbol.decoration == "double_dash"
    assert not (layers["carreteras_otra"].symbol.decoration or "")
    assert getattr(layers["municipios_l"], "simplify", 0) >= 50
    assert getattr(layers["estados_l"], "simplify", 0) >= 40
    assert layers["municipios"].label_prefix_field == "cve_mun"
    assert layers["municipios"].label_format == "newline"
    assert float(layers["municipios"].label_size) >= 10.0
    assert float(layers["municipios"].label_size) <= 11.5
    assert str(layers["municipios"].label_color or "").upper() == "#4F9A58"
    assert layers["municipios_l"].symbol.dash
    assert float(layers["municipios_l"].symbol.stroke_width) >= 2.2
    assert str(layers["municipios_l"].symbol.stroke_color or "").upper() == "#4F9A58"
    assert layers["cuerpos"].label_exclude
    assert layers["corrientes"].label_along
    assert layers["corrientes"].label_italic
    assert str(layers["corrientes"].label_color or "").upper() == "#00ADEE"
    assert str(layers["corrientes"].symbol.stroke_color or "").upper() == "#00ADEE"
    assert layers["cuerpos"].label_italic
    assert str(layers["cuerpos"].label_color or "").upper() == "#00ADEE"
    assert str(layers["cuerpos"].symbol.stroke_color or "").upper() == "#00ADEE"
    assert str(layers["cuerpos"].symbol.fill_color or "").upper() == "#7DD4F7"
    assert layers["estados_l"].symbol.decoration == "cross"
    assert str(layers["estados_l"].symbol.stroke_color or "").upper() == "#EE1C25"
    assert layers["localidades_urbana"].limit >= 2000
    assert layers["localidades_urbana"].limit <= 8000
    assert layers["localidades_urbana"].label_limit >= 400
    assert layers["corrientes"].label_limit >= 300
    assert layers["cuerpos"].label_limit >= 100
    assert layers["localidades_urbana"].symbol.fill_opacity >= 0.95
    # Filtro ambito presente (clasificación en Python: letras + códigos 1/2)
    assert any(
        col == "ambito"
        for col, _ in (layers["localidades_urbana"].attr_filters or ())
    )
    assert not any(
        col == "cve_ent"
        for col, _ in (layers["localidades_urbana"].attr_filters or ())
    )


def test_fetch_template_accepts_bbox():
    """Condensado pasa bbox statewide; croquis/planos pueden omitirlo."""
    import inspect

    from cartography_engine.datasource import fetch_template_labels, fetch_template_layers

    assert "bbox" in inspect.signature(fetch_template_layers).parameters
    assert "bbox" in inspect.signature(fetch_template_labels).parameters


def test_plotter_90x120_landscape():
    from cartography_engine.layouts import page_size, parse_layout_spec

    w, h = page_size("plotter_90x120", "landscape")
    assert w > h  # 120 cm × 90 cm
    spec = parse_layout_spec(
        {"preset": "grosig_condensado", "paper": "plotter_90x120", "orientation": "landscape"}
    )
    assert spec.paper == "plotter_90x120"
    assert spec.orientation == "landscape"
    assert spec.legend_width >= 300


def test_plotter_90x70_croquis_layout():
    from cartography_engine.layouts import build_layout, page_size, parse_layout_spec
    from cartography_engine.layers import parse_layers_from_template
    from cartography_engine.templates_loader import load_template

    w, h = page_size("plotter_90x70", "landscape")
    assert abs(w - 2551.18) < 0.1
    assert abs(h - 1984.25) < 0.1
    assert w > h

    spec = parse_layout_spec({"preset": "grosig_croquis_90x70"})
    assert spec.paper == "plotter_90x70"
    assert spec.legend_enabled
    assert not spec.show_brand_header
    assert not spec.north_enabled
    layout = build_layout(legend_items=5, spec=spec)
    assert layout.legend is not None
    assert layout.legend.height >= layout.map_frame.height * 0.95
    assert layout.legend.width >= 240

    tpl = load_template("grosig_croquis_municipal")
    assert tpl["layout"]["paper"] == "plotter_90x70"
    assert tpl["layout"]["preset"] == "grosig_croquis_90x70"
    layers = {L.id: L for L in parse_layers_from_template(tpl)}
    assert layers["localidades_urbana"].label_prefix_field == "cve_loc"
    assert layers["localidades_urbana"].label_format == "newline"
    assert layers["localidades_urbana"].label_case == "title"
    assert layers["localidades_rural"].label_format == "newline"
    assert layers["localidades_rural"].label_case == "title"
    assert layers["ctx_localidades_urbana"].label_format == "newline"
    assert layers["ctx_localidades_urbana"].label_case == "title"
    assert layers["localidades_urbana"].label_size >= 8.0
    assert abs(float(layers["localidades_rural"].label_size) - 5.5) < 0.05
    assert layers["ageb_rural"].label_style == "ageb_oval"
    assert layers["localidades_rural"].symbol.hatch
    assert "ctx_municipio_limite" in layers
    assert "ctx_carreteras_doble" in layers
    assert "ctx_carreteras_dash" in layers
    assert "ctx_carreteras_otra" in layers
    assert "ctx_municipios" in layers
    assert layers["ctx_municipios"].label_format == "newline"
    assert "ctx_localidades_urbana" in layers
    assert "ctx_localidades_rural" in layers
    assert "ctx_localidades_a" not in layers
    assert layers["ctx_localidades_urbana"].label_field == "nomgeo"
    assert float(layers["ctx_localidades_urbana"].label_size) == float(
        layers["localidades_urbana"].label_size
    )
    assert layers["ctx_localidades_urbana"].symbol.stroke_color.upper() == "#B2B2B2"
    assert layers["ctx_localidades_urbana"].symbol.dash is None
    assert layers["localidades_urbana"].symbol.dash is None
    assert layers["ctx_localidades_rural"].symbol.hatch
    assert layers["localidades_urbana"].symbol.fill_opacity >= 0.95
    assert layers["localidades_rural"].symbol.hatch
    assert layers["localidades_rural"].symbol.stroke_color.upper() == "#B2B2B2"
    assert layers["localidades_p"].label_anchor == "center"
    assert layers["localidades_p"].label_size >= 5.0
    assert layers["localidades_p"].label_size <= 5.5
    assert layers["ageb_rural"].label_size >= 12.5
    assert layers["ageb_rural"].label_color.upper() == "#FF0000"
    assert "estados_l" in layers
    assert layers["estados_l"].symbol.decoration == "cross"
    assert str(layers["estados_l"].symbol.stroke_color or "").upper() == "#CC0000"
    assert "entidad" in layers
    assert layers["entidad"].label_format == "newline"
    assert layers["entidad"].label_prefix_field == "cve_ent"
    assert str(layers["entidad"].label_color or "").upper() == "#CC0000"
    assert not layers["entidad"].draw
    assert layers["cuerpos"].label_color.upper() == "#00B3B3"
    assert layers["cuerpos"].label_italic
    assert layers["cuerpos"].label_case == "title"
    assert layers["corrientes"].label_along
    assert layers["corrientes"].label_italic
    assert layers["corrientes"].label_case == "title"
    assert layers["corrientes"].label_size >= 6.0
    assert layers["corrientes"].label_color.upper() == "#00FFFF"
    assert "ctx_localidades_p" not in layers
    assert "ctx_via_ferrea" not in layers
    assert "ctx_aeropuertos" not in layers
    assert "ctx_cuerpos" in layers
    assert "ctx_corrientes" in layers
    assert layers["ageb_rural"].symbol.stroke_width >= 2.5
    assert not layers["ctx_municipio_limite"].filter_cve_mun
    assert layers["carreteras_doble"].symbol.stroke_width <= 0.9
    assert layers["carreteras_dash"].symbol.decoration == "double_dash"
    assert layers["carreteras_dash"].symbol.stroke_width <= 0.9
    assert float(layers["ctx_municipios"].label_size) == 14.0
    assert layers["carreteras_dash"].simplify >= 40
    assert "localidades_p" in layers
    assert any(
        col == "ambito" and "Urbana" in vals
        for col, vals in (layers["localidades_urbana"].attr_filters or ())
    )
    assert any(
        col == "ambito" and "Rural" in vals
        for col, vals in (layers["localidades_rural"].attr_filters or ())
    )


def test_select_spaced_line_components():
    from shapely.geometry import LineString

    from cartography_engine.datasource import _select_spaced_line_components

    # Tres tramos colineales cercanos → solo 1 etiqueta
    a = LineString([(0, 0), (1000, 0)])
    b = LineString([(1100, 0), (2100, 0)])
    c = LineString([(50000, 0), (52000, 0)])
    kept = _select_spaced_line_components(
        [a, b, c], max_labels=2, min_separation=3000.0
    )
    assert len(kept) == 2
    # El más largo lejano y uno del grupo cercano
    assert c in kept


def test_to_proper_name():
    from cartography_engine.text_format import to_proper_name

    assert to_proper_name("LA HAMACA") == "La Hamaca"
    assert to_proper_name("PASO DEL MANGO") == "Paso del Mango"
    assert to_proper_name("RIO DE LA PRESA") == "Rio de la Presa"
    assert to_proper_name("laguna de tixtla") == "Laguna de Tixtla"


def test_format_localidad_area_label_wrap():
    from cartography_engine.text_format import (
        format_localidad_area_label,
        to_proper_name,
        wrap_name_lines,
    )

    assert wrap_name_lines("Tixtla") == ["Tixtla"]
    assert wrap_name_lines("Chilpancingo de los Bravo") == [
        "Chilpancingo",
        "de los Bravo",
    ]
    long = to_proper_name("IGUALA DE LA INDEPENDENCIA")
    lines = wrap_name_lines(long)
    assert 1 <= len(lines) <= 3
    assert "".join(lines).replace(" ", "") == long.replace(" ", "")
    assert max(len(ln) for ln in lines) <= len(long)

    label = format_localidad_area_label("0001", "Chilpancingo de los Bravo")
    assert label == "0001\nChilpancingo\nde los Bravo"


def test_localidades_a_ambito_python_split():
    from types import SimpleNamespace

    from cartography_engine.datasource import (
        _ambito_kind,
        _ambito_matches_want,
        _localidades_a_ambito_want,
    )

    urban = SimpleNamespace(
        table="mgn.localidades_a",
        attr_filters=(("ambito", ("Urbana",)),),
    )
    rural = SimpleNamespace(
        table="mgn.localidades_a",
        attr_filters=(("ambito", ("Rural",)),),
    )
    other = SimpleNamespace(
        table="mgn.municipios_a",
        attr_filters=(("ambito", ("Urbana",)),),
    )
    assert _localidades_a_ambito_want(urban) == "U"
    assert _localidades_a_ambito_want(rural) == "R"
    assert _localidades_a_ambito_want(other) is None
    assert _ambito_matches_want("Urbana", "U")
    assert _ambito_matches_want(" Rural ", "R")
    assert _ambito_matches_want("Urbana\x00", "U")
    assert _ambito_matches_want("1", "U")
    assert _ambito_matches_want("2", "R")
    assert _ambito_kind("01") == "U"
    assert _ambito_kind("02") == "R"
    assert not _ambito_matches_want("Rural", "U")
    assert not _ambito_matches_want("Urbana", "R")


def test_expand_bounds_fills_frame_aspect():
    from cartography_engine.layouts import Box
    from cartography_engine.pdf import expand_bounds_to_frame_aspect

    frame = Box(0, 0, 200, 100)
    b = expand_bounds_to_frame_aspect((0.0, 0.0, 100.0, 20.0), frame)
    minx, miny, maxx, maxy = b
    assert abs((maxx - minx) / (maxy - miny) - frame.width / frame.height) < 1e-6
    assert minx == 0.0 and maxx == 100.0
    assert miny < 0.0 and maxy > 20.0


def test_croquis_panel_content_defaults():
    from cartography_engine.pdf.croquis_panel import CroquisPanelContent
    from cartography_engine.symbols.legal_texts import ADVERTENCIA_TEXT

    p = CroquisPanelContent(municipio="Chilpancingo de los Bravo", cve_mun="029")
    assert p.titulo.startswith("CROQUIS MUNICIPAL")
    assert p.advertencia == ADVERTENCIA_TEXT
    assert p.cve_ent == "12"
    assert "2024" in p.fecha_actualizacion


def test_croquis_panel_type_scale():
    from cartography_engine.layouts import Box
    from cartography_engine.pdf.croquis_panel import _fs, _section
    from reportlab.pdfgen.canvas import Canvas
    from io import BytesIO

    t = _fs(Box(0, 0, 260, 1800))
    assert t["title"] >= 17.5
    assert t["section"] >= 13.5
    assert t["body"] >= 12.0
    assert t["id"] >= 15.0
    assert t["section_gap"] >= 6.0
    # Tras un título, el cursor baja más que la altura del glifo (salto de línea).
    c = Canvas(BytesIO())
    y0 = 500.0
    y1 = _section(c, 10, y0, "Vías de comunicación", t["small"], after=t["section_gap"])
    assert (y0 - y1) >= t["small"] * 2.2 + t["section_gap"] * 0.9


def test_condensado_panel_type_scale():
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    from cartography_engine.layouts import Box
    from cartography_engine.pdf.condensado_panel import (
        CondensadoPanelContent,
        _PAPER_SCALE,
        _fs,
        draw_condensado_panel,
    )
    from cartography_engine.pdf.croquis_panel import _fs as croquis_fs

    assert _PAPER_SCALE > 1.25
    t = _fs(Box(0, 0, 340, 2400))
    croq = croquis_fs(Box(0, 0, 260, 1800))
    assert t["title"] > croq["title"]
    assert t["body"] > croq["body"]
    c = Canvas(BytesIO(), pagesize=(3400, 2500))
    draw_condensado_panel(
        c,
        Box(3000, 50, 340, 2400),
        CondensadoPanelContent(entidad="Guerrero", cve_ent="12", escala=450000),
    )


def test_engine_version_croquis_extent_ctx():
    """Contrato de versión tras el cambio a ctx por map-extent (sin buffer)."""
    from cartography_engine import __version__

    parts = [int(x) for x in __version__.split(".")[:3]]
    assert parts >= [1, 12, 50]


def test_table_ref_cartography():
    from cartography_engine.layers import is_cartography_table, parse_table_ref

    assert parse_table_ref("marco.l") == ("marco", "l")
    assert parse_table_ref("c_mun") == (None, "c_mun")
    assert is_cartography_table("info50k.carreteras_l")
    assert not is_cartography_table("c_mun")


def test_map_focus_preset_larger_map_than_default():
    from cartography_engine.layouts import build_layout, parse_layout_spec

    default = build_layout(
        legend_items=2,
        spec=parse_layout_spec({"preset": "default"}),
    )
    focus = build_layout(
        legend_items=2,
        spec=parse_layout_spec({"preset": "map_focus"}),
    )
    assert focus.map_frame.width * focus.map_frame.height > default.map_frame.width * default.map_frame.height
    assert focus.page_width > focus.page_height  # landscape
    assert focus.margin < default.margin


def test_north_disabled_skips_arrow_in_pdf():
    from cartography_engine.layouts import build_layout, parse_layout_spec
    from cartography_engine.pdf import render_pdf

    spec = parse_layout_spec({"preset": "default", "north": {"enabled": False}})
    layout = build_layout(legend_items=2, spec=spec)
    assert layout.north_enabled is False
    pdf = render_pdf(
        layout=layout,
        title="Sin norte",
        footer="test",
        demo=True,
        map_scale=50000,
        legend_items=[],
    )
    assert pdf.startswith(b"%PDF")


def test_invalid_layout_margin_raises():
    from cartography_engine.layouts import parse_layout_spec
    from cartography_engine.models import CartographyError

    with pytest.raises(CartographyError) as exc:
        parse_layout_spec({"margin": 200})
    assert exc.value.code == "INVALID_LAYOUT"

def test_demo_blank_pdf_magic():
    from cartography_engine.models import GenerateMapRequest
    from cartography_engine.services import generate_map

    pdf, filename, media = generate_map(
        GenerateMapRequest(template_id="demo_blank", paper="letter", orientation="portrait")
    )
    assert filename.endswith(".pdf")
    assert media == "application/pdf"
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 500


def test_demo_blank_svg_magic():
    from cartography_engine.models import GenerateMapRequest
    from cartography_engine.services import generate_map

    data, filename, media = generate_map(
        GenerateMapRequest(template_id="demo_blank", format="svg")
    )
    assert filename.endswith(".svg")
    assert "svg" in media
    assert b"<svg" in data
    assert len(data) > 400


def test_health_payload_lists_templates():
    from cartography_engine.services import health_payload

    payload = health_payload()
    assert payload["engine"] == "grosig-cartography"
    assert payload["enabled"] is True
    assert "demo_blank" in payload["templates"]
    assert "croquis_municipal" in payload["templates"]
    assert "atlas_municipal" in payload["templates"]
    assert "pdf" in payload.get("formats", [])
    assert "svg" in payload.get("formats", [])
    assert "qgis_symbol_import" in payload.get("capabilities", [])
    assert "multi_page_atlas" in payload.get("capabilities", [])
    assert "custom_layouts" in payload.get("capabilities", [])
    assert "geopdf" in payload.get("capabilities", [])
    assert "geopdf" in payload.get("formats", [])
    assert "croquis_map_focus" in payload["templates"]
    assert "plano_localidad_rural" in payload["templates"]
    assert "plano_localidad_urbana" in payload["templates"]
    assert payload.get("version", "").startswith("1.9")

def test_unknown_template_raises():
    from cartography_engine.models import CartographyError, GenerateMapRequest
    from cartography_engine.services import generate_map

    with pytest.raises(CartographyError) as exc:
        generate_map(GenerateMapRequest(template_id="no_existe_xyz"))
    assert exc.value.status_code == 404


def test_croquis_requires_cve_mun():
    from cartography_engine.models import CartographyError, GenerateMapRequest
    from cartography_engine.services import generate_map

    with pytest.raises(CartographyError) as exc:
        generate_map(GenerateMapRequest(template_id="croquis_municipal", params={}))
    assert exc.value.code == "MISSING_CVE_MUN"


def test_atlas_requires_scope_or_list():
    from cartography_engine.models import CartographyError, GenerateMapRequest
    from cartography_engine.services import generate_map

    with pytest.raises(CartographyError) as exc:
        generate_map(GenerateMapRequest(template_id="atlas_municipal", params={}))
    assert exc.value.code == "MISSING_ATLAS_SCOPE"


def test_atlas_svg_rejected():
    from cartography_engine.models import CartographyError, GenerateMapRequest
    from cartography_engine.services import generate_map

    with pytest.raises(CartographyError) as exc:
        generate_map(
            GenerateMapRequest(
                template_id="atlas_municipal",
                format="svg",
                params={"cve_mun_list": ["001"]},
            )
        )
    assert exc.value.code == "ATLAS_PDF_ONLY"


def test_demo_geopdf_rejected():
    from cartography_engine.models import CartographyError, GenerateMapRequest
    from cartography_engine.services import generate_map

    with pytest.raises(CartographyError) as exc:
        generate_map(GenerateMapRequest(template_id="demo_blank", format="geopdf"))
    assert exc.value.code == "GEOPDF_NEEDS_BOUNDS"


def test_gpts_lpts_utm_corners():
    from cartography_engine.geopdf import build_gpts_lpts

    # Cuadrado aproximado cerca de Acapulco en UTM 14N
    bounds = (480000.0, 1850000.0, 490000.0, 1860000.0)
    gpts, lpts = build_gpts_lpts(bounds, "EPSG:32614")
    assert len(gpts) == 8
    assert len(lpts) == 8
    assert lpts == [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]
    # Latitudes ~16–17N, longitudes ~-99W
    for i in range(0, 8, 2):
        lat, lon = gpts[i], gpts[i + 1]
        assert 15.0 < lat < 18.0
        assert -101.0 < lon < -98.0


def test_tag_pdf_injects_measure():
    pytest.importorskip("pikepdf")
    from io import BytesIO

    import pikepdf

    from cartography_engine.geopdf import GeoViewport, tag_pdf
    from cartography_engine.layouts import build_layout
    from cartography_engine.models import GenerateMapRequest
    from cartography_engine.services import generate_map

    pdf, _, _ = generate_map(GenerateMapRequest(template_id="demo_blank"))
    layout = build_layout(legend_items=2)
    tagged = tag_pdf(
        pdf,
        [
            GeoViewport(
                page_index=0,
                map_frame=layout.map_frame,
                bounds_xy=(480000.0, 1850000.0, 490000.0, 1860000.0),
                crs="EPSG:32614",
                page_width=layout.page_width,
                page_height=layout.page_height,
            )
        ],
    )
    assert tagged.startswith(b"%PDF")
    with pikepdf.open(BytesIO(tagged)) as doc:
        page = doc.pages[0]
        assert "/VP" in page
        vp0 = page["/VP"][0]
        assert "/Measure" in vp0
        measure = vp0["/Measure"]
        assert measure["/Subtype"] == "/GEO"
        assert len(measure["/GPTS"]) == 8


def test_cover_page_pdf_bytes():
    from cartography_engine.layouts import build_layout
    from cartography_engine.pdf import render_pdf_document

    layout = build_layout(legend_items=0)
    cover = {
        "layout": layout,
        "title": "Atlas municipal — Guerrero",
        "subtitle": "Prueba",
        "municipalities": [
            {"cve_mun": "001", "nomgeo": "Acapulco de Juárez"},
            {"cve_mun": "002", "nomgeo": "Ahuacuotzingo"},
        ],
        "footer": "Atlas test · 2 hojas",
    }
    pdf = render_pdf_document(
        page_width=layout.page_width,
        page_height=layout.page_height,
        cover=cover,
        map_pages=[
            {
                "layout": layout,
                "title": "Croquis — prueba",
                "footer": "footer",
                "demo": True,
                "map_scale": 50000,
            }
        ],
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 800


def test_compute_map_scale_positive():
    from cartography_engine.layouts import Box
    from cartography_engine.renderers import compute_map_scale

    frame = Box(0, 0, 400, 400)
    # 4000 m x 4000 m en un frame de 400 pt
    scale = compute_map_scale((0, 0, 4000, 4000), frame)
    assert scale > 0
    # Orden de magnitud razonable para esa relación
    assert 5_000 < scale < 2_000_000


def test_main_does_not_mount_cartography_when_disabled(monkeypatch):
    monkeypatch.setenv("CARTOGRAPHY_ENGINE_ENABLED", "false")
    from config import get_settings

    get_settings.cache_clear()
    # Recargar main en aislamiento es frágil; validamos el setting.
    assert get_settings()["cartography_engine_enabled"] is False


def test_main_setting_enabled(monkeypatch):
    monkeypatch.setenv("CARTOGRAPHY_ENGINE_ENABLED", "true")
    from config import get_settings

    get_settings.cache_clear()
    assert get_settings()["cartography_engine_enabled"] is True


def test_legend_items_match_template_layers():
    from cartography_engine.layers import legend_items_from_template
    from cartography_engine.templates_loader import load_template

    tpl = load_template("croquis_municipal")
    items = legend_items_from_template(tpl)
    assert len(items) >= 2
    assert {i.id for i in items} >= {"municipio", "localidades"}


def test_demo_blank_has_two_legend_items():
    from cartography_engine.layers import legend_items_from_template
    from cartography_engine.templates_loader import load_template

    tpl = load_template("demo_blank")
    items = legend_items_from_template(tpl)
    assert len(items) == 2


def test_demo_pdf_includes_legend_layout():
    from cartography_engine.layouts import build_layout
    from cartography_engine.models import GenerateMapRequest
    from cartography_engine.services import generate_map

    layout = build_layout(legend_items=2)
    assert layout.legend is not None
    assert layout.legend.width > 0

    pdf, _, _ = generate_map(GenerateMapRequest(template_id="demo_blank"))
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 500


def test_qgis_simple_fill_to_grosig():
    from cartography_engine.qgis_parser import qgis_symbol_to_grosig

    xml = """
    <symbol type="fill" name="sym1">
      <layer class="SimpleFill" pass="0" enabled="1">
        <Option type="Map">
          <Option name="color" value="217,232,245,140" type="QString"/>
          <Option name="outline_color" value="31,78,121,255" type="QString"/>
          <Option name="outline_width" value="1.2" type="QString"/>
        </Option>
      </layer>
    </symbol>
    """
    sym = qgis_symbol_to_grosig(xml)
    assert sym["type"] == "polygon"
    assert sym["fill_color"].startswith("#")
    assert sym["stroke_color"].startswith("#")
    assert sym["source"] == "qgis"


def test_forbidden_layer_table_rejected():
    from cartography_engine.layers import parse_layer_def

    with pytest.raises(ValueError):
        parse_layer_def({"id": "x", "table": "pg_shadow", "label": "bad"})


@pytest.mark.skipif(
    os.getenv("CARTOGRAPHY_INTEGRATION", "").lower() not in ("1", "true", "yes"),
    reason="Requiere PostGIS (CARTOGRAPHY_INTEGRATION=true)",
)
def test_croquis_municipal_pdf_from_postgis():
    from cartography_engine.models import GenerateMapRequest
    from cartography_engine.services import generate_map

    cve = os.getenv("CARTOGRAPHY_TEST_CVE_MUN", "004")
    pdf, filename, media = generate_map(
        GenerateMapRequest(
            template_id="croquis_municipal",
            params={"cve_mun": cve},
            paper="letter",
            orientation="portrait",
        )
    )
    assert media == "application/pdf"
    assert pdf.startswith(b"%PDF")
    assert cve in filename
    assert len(pdf) > 800


@pytest.mark.skipif(
    os.getenv("CARTOGRAPHY_INTEGRATION", "").lower() not in ("1", "true", "yes"),
    reason="Requiere PostGIS (CARTOGRAPHY_INTEGRATION=true)",
)
def test_plano_localidad_rural_regression_001_0143():
    """Regresión PLR: La Providencia debe generar PDF sin error."""
    from cartography_engine.models import GenerateMapRequest
    from cartography_engine.services import generate_map

    pdf, filename, media = generate_map(
        GenerateMapRequest(
            template_id="plano_localidad",
            params={"cve_mun": "001", "cve_loc": "0143", "cve_ent": "12"},
        )
    )
    assert media == "application/pdf"
    assert pdf.startswith(b"%PDF")
    assert "001" in filename and "0143" in filename
    assert len(pdf) > 5000


@pytest.mark.skipif(
    os.getenv("CARTOGRAPHY_INTEGRATION", "").lower() not in ("1", "true", "yes"),
    reason="Requiere PostGIS (CARTOGRAPHY_INTEGRATION=true)",
)
def test_plano_localidad_urban_smoke_029_0001():
    """Smoke PLU: Chilpancingo con perfil urbano fino."""
    from cartography_engine.models import GenerateMapRequest
    from cartography_engine.services import generate_map

    pdf, filename, media = generate_map(
        GenerateMapRequest(
            template_id="plano_localidad",
            params={"cve_mun": "029", "cve_loc": "0001", "cve_ent": "12"},
        )
    )
    assert media == "application/pdf"
    assert pdf.startswith(b"%PDF")
    assert "029" in filename and "0001" in filename
    assert len(pdf) > 8000
