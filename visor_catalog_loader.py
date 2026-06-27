"""Carga el catálogo data-driven del Visor geográfico (config/visor/catalog.json)."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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


@lru_cache
def load_visor_catalog_raw() -> Dict[str, Any]:
    path = _resolve_catalog_path()
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "layers" not in data:
        raise ValueError(f"Catálogo inválido en {path}: falta objeto 'layers'")
    return data


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


def spatial_analysis_capas_from_visor_catalog() -> Dict[str, Dict[str, Any]]:
    """Capas DENUE y CLUES con capabilities.spatial_analysis en catalog.json."""
    raw = load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    out: Dict[str, Dict[str, Any]] = {}

    for layer_id, entry in layers.items():
        if not isinstance(entry, dict):
            continue
        if not (entry.get("capabilities") or {}).get("spatial_analysis"):
            continue

        key = str(layer_id).strip().lower()
        data = entry.get("data") or {}
        filt = data.get("filter") or {}
        codigos = filt.get("codigo_act")
        table = str(data.get("table") or "").strip().lower()

        # DENUE: subcapas c_denue filtradas por codigo_act (ya no usan renderer overlay_denue).
        if table == T_DENUE and codigos:
            out[key] = {
                "id": key,
                "tabla": data.get("table") or T_DENUE,
                "etiqueta": entry.get("label") or key,
                "descripcion": f"DENUE — {entry.get('label') or key}",
                "geom_column": "the_geom",
                "modo": "conteo",
                "grupo": "denue",
                "codigo_act": [int(c) for c in codigos],
            }
            continue

        if key == "clues":
            out["clues"] = {
                "id": "clues",
                "tabla": data.get("table") or T_CLUES,
                "etiqueta": entry.get("label") or "Establecimientos de salud",
                "descripcion": "Establecimientos de salud (atlas.c_clues)",
                "geom_column": "the_geom",
                "modo": "conteo",
                "grupo": "salud",
            }

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
