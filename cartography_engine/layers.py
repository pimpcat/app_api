"""Definición data-driven de capas y leyenda (GroSIG Symbol mínimo)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from cartography_engine.symbols import (
    LineSymbol,
    PointSymbol,
    PolygonSymbol,
    line_symbol_from_dict,
    point_symbol_from_dict,
    polygon_symbol_from_dict,
)

Symbol = Union[PolygonSymbol, LineSymbol, PointSymbol]
GeomKind = Literal["polygon", "line", "point", "auto"]

# Referencias permitidas: tabla Atlas (bare) o schema.tabla (GroSIG_Cartography).
ALLOWED_LAYER_TABLES = frozenset(
    {
        # Atlas
        "c_mun",
        "c_ent",
        "c_loc_punto",
        "c_l",
        "c_m",
        "c_a",
        "c_ar",
        "c_e",
        "c_clues",
        "c_denue",
        "c_col_ase",
        "hcorrientes",
        "hcuerpos",
        "curnivel",
        "c_rnc",
        # MGN
        "mgn.estados_a",
        "mgn.estados_l",
        "mgn.municipios_a",
        "mgn.municipios_l",
        "mgn.localidades_a",
        "mgn.localidades_p",
        "mgn.ageb_rurales_a",
        # INFO_50K
        "info50k.aeropuertos_p",
        "info50k.carreteras_l",
        "info50k.caminos_brechas_50_l",
        "info50k.via_ferrea_l",
        "info50k.cortinas_l",
        "info50k.corrientes_agua_l",
        "info50k.cuerpos_agua_a",
        # Marco localidad
        "marco.ent",
        "marco.mun",
        "marco.l",
        "marco.e",
        "marco.ea",
        "marco.m",
        "marco.a",
        "marco.pe",
        "marco.sip",
        "marco.sil",
        "marco.sia",
        "marco.cd",
        # Aux
        "aux.colindantes",
    }
)

CARTOGRAPHY_SCHEMAS = frozenset({"mgn", "info50k", "marco", "aux"})

# Columnas permitidas en filter.{col} además de cve_mun / cve_loc (plugin-safe).
ALLOWED_ATTR_FILTER_COLUMNS = frozenset(
    {
        "ambito",
        "tipo",
        "nume_carr",
        "administ",
        "nomgeo",
        "cve_ent",
        "condicion",
        "nombre",
        "geografico",
        "nomserv",
        "ambito_ageb",
        "tipope",
        "cve_ageb",
        "clase",
        "categoria",
        "codigo_m",
        "codigo",
        "rasgo",
    }
)


def parse_table_ref(table: str) -> tuple[Optional[str], str]:
    """'marco.l' → ('marco','l'); 'c_mun' → (None,'c_mun')."""
    raw = str(table or "").strip()
    if "." in raw:
        schema, name = raw.split(".", 1)
        schema = schema.strip()
        name = name.strip()
        if schema and name:
            return schema, name
    return None, raw


def is_cartography_table(table: str) -> bool:
    schema, _ = parse_table_ref(table)
    return schema in CARTOGRAPHY_SCHEMAS


@dataclass(frozen=True)
class LayerDef:
    id: str
    label: str
    table: str
    geom_column: str = "the_geom"
    filter_cve_mun: bool = False
    filter_cve_loc: bool = False
    clip_to_municipio: bool = False
    clip_to_localidad: bool = False
    limit: int = 2000
    symbol: Any = None
    geom_kind: GeomKind = "auto"
    show_in_legend: bool = True
    # False = no pintar geometría (sí pueden usarse etiquetas, p.ej. ejes vial)
    draw: bool = True
    optional: bool = False
    label_field: Optional[str] = None
    label_limit: int = 40
    label_prefix_field: Optional[str] = None
    label_suffix_field: Optional[str] = None
    label_format: str = ""  # "" | "paren" | "tipo_nom" | "newline" (área: wrap nombre)
    label_color: str = "#1a1a1a"
    label_bold: bool = False
    label_italic: bool = False
    label_size: float = 0.0  # 0 = escala automática de página
    label_exclude: tuple[str, ...] = ()
    label_style: str = ""  # p.ej. "ageb_oval" | "along"
    label_anchor: str = "auto"  # auto | center | offset
    # Tolerancia ST_SimplifyPreserveTopology en unidades del CRS de mapa (m en UTM).
    simplify: float = 0.0
    # Filtros atributo: ((columna, (valor1, valor2, ...)), ...)
    attr_filters: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Exclusiones atributo (NOT IN): ((columna, (valor1, ...)), ...)
    attr_excludes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    label_along: bool = False  # texto paralelo a la línea (SIL / vialidades)
    label_case: str = ""  # "" | "title" | "proper" — nombres propios

@dataclass(frozen=True)
class LegendItem:
    id: str
    label: str
    kind: Literal["polygon", "line", "point"]
    symbol: Symbol


@dataclass(frozen=True)
class LayerData:
    definition: LayerDef
    geometry: Any  # shapely BaseGeometry | None
    feature_count: int


@dataclass(frozen=True)
class LabelPlacement:
    text: str
    geometry: Any
    layer_id: str = ""


def _symbol_from_layer_dict(data: dict[str, Any]) -> tuple[Symbol, GeomKind]:
    raw = data.get("symbol") if isinstance(data.get("symbol"), dict) else {}
    kind = str(raw.get("type") or data.get("geometry") or data.get("geom_kind") or "auto").lower()
    table = str(data.get("table") or "")
    if kind in ("none", "hidden", "invisible"):
        # Capa solo-etiquetas: símbolo dummy (no se pinta)
        geom = str(data.get("geometry") or "line").lower()
        if geom in ("line", "linestring", "multilinestring"):
            return line_symbol_from_dict({"stroke": "#FFFFFF", "width": 0}), "line"
        if geom in ("point", "multipoint"):
            return point_symbol_from_dict({"size": 0}), "point"
        return polygon_symbol_from_dict({"stroke_width": 0}), "polygon"
    if kind in ("line", "linestring", "multilinestring"):
        return line_symbol_from_dict(raw), "line"
    if kind in ("point", "multipoint"):
        return point_symbol_from_dict(raw), "point"
    if kind in ("polygon", "multipolygon", "fill"):
        return polygon_symbol_from_dict(raw), "polygon"
    pointish = table in (
        "c_loc_punto",
        "c_clues",
        "c_denue",
        "mgn.localidades_p",
        "info50k.aeropuertos_p",
        "marco.sip",
        "marco.cd",
    )
    if "size" in raw or (kind == "auto" and pointish):
        return point_symbol_from_dict(raw), "point" if pointish else "auto"
    return polygon_symbol_from_dict(raw), "auto"


def parse_layer_def(data: dict[str, Any], index: int = 0) -> LayerDef:
    lid = str(data.get("id") or f"layer_{index}").strip()
    label = str(data.get("label") or lid).strip()
    table = str(data.get("table") or "").strip()
    if not table:
        raise ValueError(f"Capa {lid}: falta table")
    if table not in ALLOWED_LAYER_TABLES:
        raise ValueError(f"Capa {lid}: tabla no permitida: {table}")

    filt = data.get("filter") if isinstance(data.get("filter"), dict) else {}
    filter_cve = bool(
        data.get("filter_cve_mun")
        or filt.get("cve_mun") in ("{cve_mun}", True, "cve_mun")
        or str(filt.get("cve_mun") or "").find("{cve_mun}") >= 0
    )
    if "cve_mun" in filt:
        filter_cve = True

    filter_loc = bool(
        data.get("filter_cve_loc")
        or filt.get("cve_loc") in ("{cve_loc}", True, "cve_loc")
        or str(filt.get("cve_loc") or "").find("{cve_loc}") >= 0
    )
    if "cve_loc" in filt:
        filter_loc = True

    clip_loc = bool(
        data.get("clip_to_localidad")
        or data.get("clip_to") in ("localidad", "l", "pe")
    )
    clip_mun = bool(
        data.get("clip_to_municipio")
        or data.get("clip_to") == "municipio"
    )

    symbol, kind = _symbol_from_layer_dict(data)
    # draw:false | visible:false | symbol.type=none → solo etiquetas / sin trazo
    raw_sym = data.get("symbol") if isinstance(data.get("symbol"), dict) else {}
    draw = True
    if data.get("draw") is False or data.get("visible") is False or data.get("paint") is False:
        draw = False
    if str(raw_sym.get("type") or "").lower() in ("none", "hidden", "invisible"):
        draw = False
    try:
        if float(raw_sym.get("width", raw_sym.get("stroke_width", 1))) <= 0:
            draw = False
    except (TypeError, ValueError):
        pass
    labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
    label_field = None
    label_limit = 40
    label_prefix_field = None
    label_suffix_field = None
    label_format = ""
    label_color = "#1a1a1a"
    label_bold = False
    label_italic = False
    label_size = 0.0
    label_exclude: tuple[str, ...] = ()
    label_style = ""
    label_anchor = "auto"
    label_along = False
    label_case = ""
    if labels.get("enabled", False) or labels.get("field"):
        label_field = str(labels.get("field") or "").strip() or None
        try:
            label_limit = max(1, min(int(labels.get("limit") or 40), 50000))
        except (TypeError, ValueError):
            label_limit = 40
        prefix = labels.get("prefix_field") or labels.get("prefix")
        if prefix:
            label_prefix_field = str(prefix).strip() or None
        suffix = labels.get("suffix_field") or labels.get("suffix")
        if suffix:
            label_suffix_field = str(suffix).strip() or None
        fmt = str(labels.get("format") or "").strip().lower()
        if fmt in ("paren", "name_code", "nom_cve", "paren_suffix"):
            label_format = "paren"
        elif fmt in ("tipo_nom", "tiponom", "tipo+nom", "tipo_nomvial"):
            label_format = "tipo_nom"
        elif fmt in ("newline", "stack", "clave_nl", "clave_nombre", "nl"):
            label_format = "newline"
        if labels.get("color"):
            label_color = str(labels.get("color")).strip() or "#1a1a1a"
        label_bold = bool(labels.get("bold", False))
        label_italic = bool(
            labels.get("italic")
            or labels.get("oblique")
            or str(labels.get("font_style") or "").lower() in ("italic", "oblique")
        )
        case_raw = str(labels.get("case") or labels.get("text_case") or "").strip().lower()
        if case_raw in ("title", "proper", "nombre", "name"):
            label_case = "title"
        try:
            label_size = max(0.0, min(float(labels.get("size") or 0), 72.0))
        except (TypeError, ValueError):
            label_size = 0.0
        excl = labels.get("exclude") or labels.get("exclude_values")
        if isinstance(excl, (list, tuple)):
            label_exclude = tuple(str(x).strip().upper() for x in excl if str(x).strip())
        elif excl:
            label_exclude = (str(excl).strip().upper(),)
        style = str(labels.get("style") or "").strip().lower()
        if style in ("ageb_oval", "oval"):
            label_style = "ageb_oval"
        elif style in ("along", "follow", "line"):
            label_style = "along"
        label_along = bool(
            labels.get("along")
            or labels.get("follow_line")
            or label_style == "along"
        )
        if label_along:
            label_style = "along"
            label_anchor = "center"
        anchor = str(labels.get("anchor") or "").strip().lower()
        if not label_along and anchor in ("center", "offset", "auto"):
            label_anchor = anchor
        elif not label_along and (
            label_field in ("cve_mza", "CVE_MZA") or lid in ("manzanas", "manzana")
        ):
            label_anchor = "center"
        # Vialidades: nomvial (cvevial) por defecto si hay suffix
        if label_suffix_field and not label_format:
            label_format = "paren"

    try:
        simplify = max(0.0, float(data.get("simplify") or 0))
    except (TypeError, ValueError):
        simplify = 0.0
    simplify = min(simplify, 500.0)

    attr_filters: list[tuple[str, tuple[str, ...]]] = []
    for key, raw_val in filt.items():
        col = str(key or "").strip().lower()
        if col in ("cve_mun", "cve_loc") or col not in ALLOWED_ATTR_FILTER_COLUMNS:
            continue
        values: list[str] = []
        if isinstance(raw_val, (list, tuple)):
            values = [str(v).strip() for v in raw_val if str(v).strip()]
        elif raw_val is not None and raw_val is not True and raw_val is not False:
            s = str(raw_val).strip()
            if s and "{" not in s:
                values = [s]
        if values:
            attr_filters.append((col, tuple(values)))

    attr_excludes: list[tuple[str, tuple[str, ...]]] = []
    excl_filt = data.get("filter_exclude")
    if isinstance(excl_filt, dict):
        for key, raw_val in excl_filt.items():
            col = str(key or "").strip().lower()
            if col not in ALLOWED_ATTR_FILTER_COLUMNS:
                continue
            values: list[str] = []
            if isinstance(raw_val, (list, tuple)):
                values = [str(v).strip() for v in raw_val if str(v).strip()]
            elif raw_val is not None and raw_val is not True and raw_val is not False:
                s = str(raw_val).strip()
                if s and "{" not in s:
                    values = [s]
            if values:
                attr_excludes.append((col, tuple(values)))

    return LayerDef(
        id=lid,
        label=label,
        table=table,
        geom_column=str(data.get("geom_column") or "the_geom").strip() or "the_geom",
        filter_cve_mun=filter_cve,
        filter_cve_loc=filter_loc,
        clip_to_municipio=clip_mun,
        clip_to_localidad=clip_loc,
        limit=max(1, min(int(data.get("limit") or 2000), 50000)),
        symbol=symbol,
        geom_kind=kind,
        show_in_legend=(data.get("legend", True) is not False) and draw,
        draw=draw,
        optional=bool(data.get("optional", False)),
        label_field=label_field,
        label_limit=label_limit,
        label_prefix_field=label_prefix_field,
        label_suffix_field=label_suffix_field,
        label_format=label_format,
        label_color=label_color,
        label_bold=label_bold,
        label_italic=label_italic,
        label_size=label_size,
        label_exclude=label_exclude,
        label_style=label_style,
        label_anchor=label_anchor,
        simplify=simplify,
        attr_filters=tuple(attr_filters),
        attr_excludes=tuple(attr_excludes),
        label_along=label_along,
        label_case=label_case,
    )


def parse_layers_from_template(template: dict[str, Any]) -> list[LayerDef]:
    raw = template.get("layers")
    if not isinstance(raw, list) or not raw:
        style = (template.get("style") or {}).get("municipio") or template.get("symbol") or {}
        ds = template.get("datasource") or {}
        return [
            LayerDef(
                id="municipio",
                label=str(ds.get("label") or "Límite municipal"),
                table=str(ds.get("table") or "c_mun"),
                geom_column=str(ds.get("geom_column") or "the_geom"),
                filter_cve_mun=True,
                clip_to_municipio=False,
                limit=1,
                symbol=polygon_symbol_from_dict(style if isinstance(style, dict) else {}),
                geom_kind="polygon",
                show_in_legend=True,
                optional=False,
            )
        ]
    return [parse_layer_def(item, i) for i, item in enumerate(raw) if isinstance(item, dict)]


def legend_items_from_layers(layers: list[LayerDef]) -> list[LegendItem]:
    items: list[LegendItem] = []
    for layer in layers:
        if not layer.show_in_legend:
            continue
        kind: Literal["polygon", "line", "point"]
        if layer.geom_kind == "line":
            kind = "line"
        elif layer.geom_kind == "point":
            kind = "point"
        else:
            kind = "polygon"
        items.append(LegendItem(id=layer.id, label=layer.label, kind=kind, symbol=layer.symbol))
    return items


def legend_items_from_template(template: dict[str, Any]) -> list[LegendItem]:
    return legend_items_from_layers(parse_layers_from_template(template))
