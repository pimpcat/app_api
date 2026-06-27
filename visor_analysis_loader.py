"""Catálogo de análisis espacial INV/ITER (config/visor/analysis_catalog.json)."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from tables import T_C_INV, T_ITER, T_LOC_PUNTO


def _analysis_catalog_search_paths() -> List[Path]:
    env = os.getenv("VISOR_ANALYSIS_CATALOG_PATH", "").strip()
    paths: List[Path] = []
    if env:
        paths.append(Path(env))
    here = Path(__file__).resolve().parent
    paths.extend(
        [
            here.parent / "config" / "visor" / "analysis_catalog.json",
            Path("/config/visor/analysis_catalog.json"),
            here / "config" / "visor" / "analysis_catalog.json",
        ]
    )
    return paths


def _resolve_analysis_catalog_path() -> Path:
    for path in _analysis_catalog_search_paths():
        if path.is_file():
            return path
    tried = ", ".join(str(p) for p in _analysis_catalog_search_paths())
    raise FileNotFoundError(f"No se encontró analysis_catalog.json. Rutas probadas: {tried}")


@lru_cache
def load_analysis_catalog_raw() -> Dict[str, Any]:
    path = _resolve_analysis_catalog_path()
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "layers" not in data:
        raise ValueError(f"analysis_catalog inválido en {path}: falta 'layers'")
    return data


def analysis_catalog_path() -> str:
    return str(_resolve_analysis_catalog_path())


def analysis_catalog_for_api() -> Dict[str, Any]:
    raw = load_analysis_catalog_raw()
    return {
        "version": raw.get("version", 1),
        "catalog_path": analysis_catalog_path(),
        "layers": raw.get("layers") or {},
    }


def _flat_fields_from_sections(sections: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(sections, list):
        return out
    for section in sections:
        if not isinstance(section, dict):
            continue
        for field in section.get("campos") or []:
            if not isinstance(field, dict):
                continue
            col = str(field.get("columna") or "").strip().lower()
            if not col:
                continue
            out.append(
                {
                    "columna": col,
                    "etiqueta": str(field.get("etiqueta") or col),
                    "agregacion": str(field.get("agregacion") or "sum"),
                }
            )
    return out


def analysis_layer_entry(layer_id: str) -> Optional[Dict[str, Any]]:
    key = (layer_id or "").strip().lower()
    if not key:
        return None
    layers = load_analysis_catalog_raw().get("layers") or {}
    entry = layers.get(key)
    return entry if isinstance(entry, dict) else None


def inv_campos_analisis() -> List[Dict[str, str]]:
    entry = analysis_layer_entry("c_inv")
    if entry:
        return _flat_fields_from_sections(entry.get("sections"))
    return []


def iter_campos_analisis() -> List[Dict[str, str]]:
    entry = analysis_layer_entry("iter")
    if entry:
        return _flat_fields_from_sections(entry.get("sections"))
    return []


def build_censales_capas_from_analysis_catalog() -> Dict[str, Dict[str, Any]]:
    """Meta INV/ITER para spatial_analysis.CAPAS_ANALISIS."""
    out: Dict[str, Dict[str, Any]] = {}
    layers = load_analysis_catalog_raw().get("layers") or {}
    table_map = {"c_inv": T_C_INV, "iter": T_ITER}

    for layer_id, entry in layers.items():
        if not isinstance(entry, dict):
            continue
        key = str(layer_id).strip().lower()
        tabla = str(entry.get("tabla") or table_map.get(key) or key)
        meta: Dict[str, Any] = {
            "id": str(entry.get("id") or key),
            "tabla": tabla,
            "etiqueta": entry.get("etiqueta") or key,
            "descripcion": entry.get("descripcion", ""),
            "geom_column": entry.get("geom_column", "the_geom"),
            "grupo": entry.get("grupo", "censales"),
            "modo": entry.get("modo", "agregacion"),
        }
        if entry.get("srid_almacenamiento") is not None:
            meta["srid_almacenamiento"] = entry["srid_almacenamiento"]
        if entry.get("geom_tabla"):
            meta["geom_tabla"] = entry["geom_tabla"]
        if entry.get("join_column"):
            meta["join_column"] = entry["join_column"]
        if key == "iter" and not meta.get("geom_tabla"):
            meta["geom_tabla"] = T_LOC_PUNTO
            meta["join_column"] = meta.get("join_column") or "cvegeo"
        out[key] = meta
    return out
