"""Carga el catálogo data-driven del Visor geográfico (config/visor/catalog.json)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config_json_errors import ConfigJsonSyntaxError, load_json_object

from visor_attribute_filter import parse_attribute_filter

from tables import T_CLUES, T_DENUE, T_RNC, qualified

from visor_analysis_loader import analysis_catalog_for_api

DENUE_KML_EXPORT_COLUMNS = [
    "gid",
    "cve_mun",
    "municipio",
    "codigo_act",
    "nom_estab",
    "nombre_act",
    "localidad",
]


def _catalog_search_paths() -> List[Path]:
    env = os.getenv("VISOR_CATALOG_PATH", "").strip()
    paths: List[Path] = []
    if env:
        paths.append(Path(env))
    here = Path(__file__).resolve().parent
    paths.extend(
        [
            here.parent / "config" / "visor" / "catalog.json",
            Path("/config/visor/catalog.json"),
            here / "config" / "visor" / "catalog.json",
        ]
    )
    return paths


def _resolve_catalog_path() -> Path:
    for path in _catalog_search_paths():
        if path.is_file():
            return path
    tried = ", ".join(str(p) for p in _catalog_search_paths())
    raise FileNotFoundError(f"No se encontró catalog.json del visor. Rutas probadas: {tried}")


_catalog_cache: Optional[Tuple[float, Dict[str, Any]]] = None


def load_visor_catalog_raw() -> Dict[str, Any]:
    """Lee catalog.json; invalida automáticamente si cambia mtime (varios workers Gunicorn)."""
    global _catalog_cache
    path = _resolve_catalog_path()
    mtime = path.stat().st_mtime_ns
    if _catalog_cache is not None and _catalog_cache[0] == mtime:
        return _catalog_cache[1]
    data = load_json_object(path)
    if not isinstance(data, dict) or "layers" not in data:
        raise ValueError(f"Catálogo inválido en {path}: falta objeto 'layers'")
    _catalog_cache = (mtime, data)
    return data


def invalidate_visor_catalog_cache() -> None:
    """Limpia caché en memoria (p. ej. tras guardar catalog.json)."""
    global _catalog_cache
    _catalog_cache = None


def catalog_path() -> str:
    return str(_resolve_catalog_path())


def _codigo_act_predicate(codigos: Sequence[int]) -> str:
    codes = ", ".join(f"'{int(c)}'" for c in codigos)
    return f"regexp_replace(TRIM(codigo_act::text), '[^0-9]', '', 'g') IN ({codes})"


def _denue_from_sql(codigos: Sequence[int]) -> str:
    where_codes = _codigo_act_predicate(codigos)
    return f"""(
        SELECT *
          FROM {qualified(T_DENUE)}
         WHERE {where_codes}
    ) AS src"""


def _rnc_from_sql() -> str:
    return f"""(
        SELECT gid, cve_mun, tipo_vial, NULL::text AS cvegeo,
               ST_Simplify(the_geom, 8.0) AS the_geom
          FROM {qualified(T_RNC)}
    ) AS src"""


def _parse_export_block(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza data.export y claves legacy a un solo dict export."""
    export = data.get("export")
    if isinstance(export, str):
        return {"mode": export.strip().lower() or "all"}
    if isinstance(export, dict):
        return dict(export)

    out: Dict[str, Any] = {}
    if data.get("shp_all_table_columns"):
        out["mode"] = "all"
    elif data.get("export_columns") or data.get("export_columns_kml"):
        out["mode"] = "columns"
    else:
        out["mode"] = "all"

    if data.get("export_columns"):
        out["columns"] = list(data["export_columns"])
    if data.get("export_columns_kml"):
        out["columns_kml"] = list(data["export_columns_kml"])
    if data.get("export_columns_shp"):
        out["columns_shp"] = list(data["export_columns_shp"])
    if data.get("export_exclude"):
        out["exclude"] = list(data["export_exclude"])
    return out


def _layer_data_to_backend(layer_id: str, layer: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte bloque `data` del catálogo al dict que espera export/buffer."""
    data = layer.get("data") or {}
    out: Dict[str, Any] = {
        "label": layer.get("label") or layer_id,
        "geom_type": layer.get("geometry") or "polygon",
    }

    if data.get("mun_filter_cvegeo") is False:
        out["mun_filter_cvegeo"] = False
    if data.get("mun_filter") is False:
        out["mun_filter"] = False

    out["export"] = _parse_export_block(data)

    # Tabla para listar columnas (all) y legacy
    export_table = data.get("export_table") or data.get("gid_table") or data.get("table")
    if export_table:
        out["export_table"] = export_table

    # Legacy: consumidores que aún lean estas claves
    export_cfg = out["export"]
    if export_cfg.get("mode") == "columns":
        if export_cfg.get("columns"):
            out["export_columns"] = list(export_cfg["columns"])
        if export_cfg.get("columns_kml"):
            out["export_columns_kml"] = list(export_cfg["columns_kml"])
    if data.get("shp_all_table_columns"):
        out["shp_all_table_columns"] = True
    if data.get("gid_table"):
        out["gid_table"] = data["gid_table"]

    preset = data.get("from_sql_preset")
    if preset == "rnc_simplified":
        out["from_sql"] = _rnc_from_sql()
    elif data.get("from_sql"):
        out["from_sql"] = str(data["from_sql"])

    filt = data.get("filter") or {}
    codigos = filt.get("codigo_act")
    if codigos:
        out["from_sql"] = _denue_from_sql(codigos)
        out["export_subquery_full"] = True
        if not out.get("gid_table"):
            out["gid_table"] = T_DENUE
        if not out.get("export_table"):
            out["export_table"] = T_DENUE
        out["mun_filter_cvegeo"] = False
        out["geom_type"] = "point"
        exp = out.get("export") or {}
        if exp.get("mode") == "columns" and not (
            exp.get("columns")
            or exp.get("columns_kml")
            or out.get("export_columns_kml")
        ):
            out["export_columns_kml"] = DENUE_KML_EXPORT_COLUMNS
            out["export"]["columns_kml"] = DENUE_KML_EXPORT_COLUMNS
    elif data.get("table"):
        out["table"] = data["table"]

    attr_f = parse_attribute_filter(data)
    if attr_f:
        out["attribute_filter"] = attr_f

    style = layer.get("style")
    if isinstance(style, dict) and style:
        out["style"] = dict(style)

    return out


def layer_catalog_from_config() -> Dict[str, Dict[str, Any]]:
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    catalog: Dict[str, Dict[str, Any]] = {}
    for layer_id, layer in layers.items():
        if not isinstance(layer, dict):
            continue
        catalog[str(layer_id).strip().lower()] = _layer_data_to_backend(layer_id, layer)
    return catalog


def denue_codigos_from_config(layer_id: str) -> Optional[Sequence[int]]:
    raw = load_visor_catalog_raw()
    layer = (raw.get("layers") or {}).get(layer_id)
    if not layer:
        layer = (raw.get("layers") or {}).get(layer_id.lower())
    if not isinstance(layer, dict):
        return None
    filt = (layer.get("data") or {}).get("filter") or {}
    codigos = filt.get("codigo_act")
    if not codigos:
        return None
    return tuple(int(c) for c in codigos)


def ordered_layer_ids_from_raw(raw: Optional[Dict[str, Any]] = None) -> List[str]:
    """Orden de capas según grupos del catálogo."""
    data = raw if raw is not None else load_visor_catalog_raw()
    layers = data.get("layers") or {}
    groups = data.get("groups") or []
    ordered_ids: List[str] = []
    for group in groups:
        for lid in group.get("layers") or []:
            if lid not in ordered_ids:
                ordered_ids.append(lid)
    for lid in layers:
        if lid not in ordered_ids:
            ordered_ids.append(lid)
    return ordered_ids


def get_layer_identify_field_names(layer_id: str) -> List[str]:
    """Columnas configuradas en identify.fields del catálogo (solo nombres)."""
    raw = load_visor_catalog_raw()
    layer = (raw.get("layers") or {}).get(layer_id)
    if not layer:
        layer = (raw.get("layers") or {}).get(str(layer_id).strip().lower())
    if not isinstance(layer, dict):
        return []
    identify = layer.get("identify") or {}
    fields = identify.get("fields") or []
    names: List[str] = []
    for item in fields:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            col = item.get("column") or item.get("field") or item.get("name")
            if col and str(col).strip():
                names.append(str(col).strip())
    return names


def _default_spatial_modo(geometry: str) -> str:
    return "conteo" if (geometry or "").strip().lower() == "point" else "agregacion"


def _catalog_layer_eligible_for_spatial_analysis(layer_id: str, entry: Dict[str, Any]) -> bool:
    """
    True si la capa debe entrar al motor de análisis espacial.

    - DENUE / CLUES: legacy (solo capabilities.spatial_analysis).
    - Capas Studio: requieren bloque spatial_analysis publicado por el wizard.
    """
    if not (entry.get("capabilities") or {}).get("spatial_analysis"):
        return False

    key = str(layer_id).strip().lower()
    data = entry.get("data") or {}
    table = str(data.get("table") or "").strip().lower()
    filt = data.get("filter") or {}

    if table == T_DENUE and filt.get("codigo_act"):
        return True
    if key == "clues" or table == T_CLUES:
        return True

    spatial = entry.get("spatial_analysis")
    if not isinstance(spatial, dict) or not spatial:
        return False

    modo = str(spatial.get("modo") or "").strip().lower()
    geometry = str(entry.get("geometry") or "polygon").strip().lower()
    if not modo:
        modo = _default_spatial_modo(geometry)

    if modo == "agregacion":
        sections = spatial.get("sections")
        if isinstance(sections, list):
            for section in sections:
                if isinstance(section, dict) and section.get("campos"):
                    return True
        return False

    if modo == "conteo":
        return True

    return False


def _spatial_detail_columns_from_entry(entry: Dict[str, Any], spatial: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Columnas detalle: spatial_analysis.detail_columns o, si vacío, tabular.columns (data-driven)."""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in spatial.get("detail_columns") or []:
        if not isinstance(item, dict):
            continue
        col = str(item.get("columna") or item.get("column") or item.get("field") or "").strip().lower()
        if not col or col in seen:
            continue
        seen.add(col)
        label = str(item.get("etiqueta") or item.get("label") or col).strip() or col
        out.append({"columna": col, "etiqueta": label})
    if out or not spatial.get("detail_table"):
        return out
    tabular = entry.get("tabular") or {}
    for item in tabular.get("columns") or []:
        if not isinstance(item, dict):
            continue
        col = str(item.get("field") or item.get("columna") or item.get("column") or "").strip().lower()
        if not col or col in seen:
            continue
        seen.add(col)
        label = str(item.get("label") or item.get("etiqueta") or col).strip() or col
        out.append({"columna": col, "etiqueta": label})
    return out


def _spatial_meta_from_catalog_entry(layer_id: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Construye meta de CAPAS_ANALISIS desde una entrada de catalog.json."""
    key = str(layer_id).strip().lower()
    data = entry.get("data") or {}
    spatial = entry.get("spatial_analysis") or {}
    geometry = str(entry.get("geometry") or "polygon").strip().lower()
    filt = data.get("filter") or {}
    codigos = filt.get("codigo_act")
    table = str(data.get("table") or "").strip().lower()

    if table == T_DENUE and codigos:
        meta: Dict[str, Any] = {
            "id": key,
            "tabla": data.get("table") or T_DENUE,
            "etiqueta": entry.get("label") or key,
            "descripcion": f"DENUE — {entry.get('label') or key}",
            "geom_column": str(spatial.get("geom_column") or "the_geom"),
            "modo": str(spatial.get("modo") or "conteo"),
            "grupo": str(spatial.get("grupo") or "denue"),
            "codigo_act": [int(c) for c in codigos],
        }
    elif key == "clues" or table == T_CLUES:
        meta = {
            "id": key if key == "clues" else key,
            "tabla": data.get("table") or T_CLUES,
            "etiqueta": entry.get("label") or "Establecimientos de salud",
            "descripcion": entry.get("descripcion") or f"{entry.get('label') or key}",
            "geom_column": str(spatial.get("geom_column") or "the_geom"),
            "modo": str(spatial.get("modo") or "conteo"),
            "grupo": str(spatial.get("grupo") or "salud"),
        }
    else:
        grupo = str(spatial.get("grupo") or "tematicas")
        meta = {
            "id": key,
            "tabla": data.get("table") or key,
            "etiqueta": entry.get("label") or key,
            "descripcion": entry.get("descripcion") or str(entry.get("label") or key),
            "geom_column": str(spatial.get("geom_column") or "the_geom"),
            "modo": str(spatial.get("modo") or _default_spatial_modo(geometry)),
            "grupo": grupo,
        }
        if spatial.get("geom_tabla"):
            meta["geom_tabla"] = str(spatial["geom_tabla"])
        if spatial.get("join_column"):
            meta["join_column"] = str(spatial["join_column"])

    if isinstance(spatial.get("sections"), list) and spatial["sections"]:
        meta["sections"] = spatial["sections"]
    if spatial.get("detail_table"):
        meta["detail_table"] = True
    detail_cols = _spatial_detail_columns_from_entry(entry, spatial)
    if detail_cols:
        meta["detail_columns"] = detail_cols
    ui = spatial.get("ui")
    if isinstance(ui, dict) and ui:
        meta["ui"] = ui

    attr_f = parse_attribute_filter(data)
    if attr_f:
        meta["attribute_filter"] = attr_f

    mun_f = data.get("mun_filter")
    if mun_f is False:
        meta["mun_filter"] = False
    elif mun_f:
        meta["mun_filter"] = mun_f
    if data.get("mun_filter_cvegeo") is False:
        meta["mun_filter_cvegeo"] = False
    elif key == "clues" or table == T_CLUES or table == T_DENUE:
        meta["mun_filter_cvegeo"] = False

    return meta


def spatial_analysis_capas_from_visor_catalog() -> Dict[str, Dict[str, Any]]:
    """Capas con análisis espacial configurado (wizard o legacy DENUE/CLUES)."""
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    out: Dict[str, Dict[str, Any]] = {}

    for layer_id, entry in layers.items():
        if not isinstance(entry, dict):
            continue
        if not _catalog_layer_eligible_for_spatial_analysis(layer_id, entry):
            continue
        key = str(layer_id).strip().lower()
        out[key] = _spatial_meta_from_catalog_entry(key, entry)

    return out


def merge_capas_analisis() -> Dict[str, Dict[str, Any]]:
    """INV/ITER (analysis_catalog) + DENUE/CLUES (catalog.json)."""
    from visor_analysis_loader import build_censales_capas_from_analysis_catalog

    capas: Dict[str, Dict[str, Any]] = {}
    capas.update(build_censales_capas_from_analysis_catalog())
    capas.update(spatial_analysis_capas_from_visor_catalog())
    return capas


def catalog_for_api() -> Dict[str, Any]:
    """Respuesta completa para GET /api/visor/catalog (panel + metadatos)."""
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    groups = raw.get("groups") or []
    ordered_ids = ordered_layer_ids_from_raw(raw)

    layer_list = []
    for lid in ordered_ids:
        entry = layers.get(lid)
        if not isinstance(entry, dict):
            continue
        caps = entry.get("capabilities") or {}
        layer_list.append(
            {
                "id": lid,
                **entry,
                "export": caps.get("export"),
                "tabular": bool(caps.get("tabular")),
                "spatial_analysis": bool(caps.get("spatial_analysis")),
            }
        )

    try:
        analysis_catalog = analysis_catalog_for_api()
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        analysis_catalog = {"version": 1, "layers": {}}

    return {
        "version": raw.get("version", 1),
        "catalog_path": catalog_path(),
        "groups": groups,
        "layers": layer_list,
        "layer_by_id": layers,
        "search": raw.get("search") or {},
        "search_extras": raw.get("search_extras") or [],
        "analysis_catalog": analysis_catalog,
    }
